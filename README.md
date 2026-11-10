# mini-SWE-agent → ARC-Bench adapter

围绕**未改动的 [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) v2.4.6**（PyPI 安装，不 vendor 源码）做的 ARC-Bench 提交适配层：逐字读取 `requirements.yaml` 与参考图片，多模态注入 prompt，从 `web_template/` 阶段化 React/Vite + Express + SQLite 起始项目，由上游 agent 用 bash 工具实现应用。

- ARC-Bench: https://github.com/code-philia/arc-bench；Runner 约定：`python main.py <requirements目录> --output-dir <应用目录>`，ZIP 根含 `main.py` + `requirements.txt`
- 历史诊断与变更记录：[docs/history.md](docs/history.md)

## 状态

入口、模板启动、离线传输与回归测试已验证（16 passed）。完整 Keep baseline 未跑通：模型端点在长输出阶段断连（httpx `RemoteProtocolError`），诊断见 docs/history.md。得分 N/A / 33（未运行）。

## 安装

```bash
unzip mini_swe_arcbench_adapter.zip -d mini_swe_arcbench_adapter
cd mini_swe_arcbench_adapter
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
mini --help   # 上游 CLI 仍可用
```

Python 3.11+（3.13 实测），Node.js 22.12+，Windows 用 WSL2。端点须支持 tool calling + 图片输入（LiteLLM `openai/<model>` provider）。

## 运行

```bash
cp .env.example .env   # 填 OPENAI_BASE_URL / OPENAI_API_KEY；shell 环境变量优先于 .env
python main.py <requirements目录> --output-dir <应用目录> --dry-run   # 不花 API：校验输入、模板、多模态准备
python main.py <requirements目录> --output-dir <应用目录>             # 真实生成
PORT=3000 bash <应用目录>/deploy.sh                                    # 前台启动，期间保持运行
bash scripts/run_keep_baseline.sh <arc-bench克隆> <全新空输出目录>      # 生成→部署→官方 Playwright 测试 一键
```

模型优先级：`--model` > `ARC_MODEL` > `MODEL` > `deepseek-flash`。
环境变量：`ARC_REQUEST_TIMEOUT`（秒，默认 600）、`MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT`（默认 100——网关错误通常不计费且失败即损失全部进度，最坏情形 ~6.6 小时重试直到平台/网关恢复）、`ARC_MAX_STEPS`（默认 250，沿用上游 swebench 配置）、`ARC_MAX_SECONDS`（3600）、`ARC_REASONING_EFFORT`（默认 `low`，传给端点的思考力度，设空省略该参数）、`ARC_TRANSPORT_REASONING`（`passthrough` 默认，按上游原样回传思考内容——DeepSeek 契约与重编码网关需要；`strip` 为显式剥离，仅在确认端点不需要且窗口/计费敏感时使用）、`ARC_LOG_IMAGES`（默认剥离审计产物中的图片 base64，`=1` 保留原图用于图片相关调试）。

Prompt 架构：harness 契约逐字沿用上游 `config/benchmarks/swebench.yaml`（system、instance 骨架、输出截断、format error、`drop_params`/`parallel_tool_calls`、step_limit 250）与 `config/default.yaml` 的 shell 环境（`PAGER=cat` 等）；本仓库只承载任务域内容（YAML、参考图、禁区、提交标记）。参考图附加前自动缩至 ≤1280px 并按更小的 JPEG/PNG 重编码（实测 19 张 13.2MB → 1.0MB，规避网关 413 请求体上限；原图在 `.arc_requirements/` 不变）。

## 日志（记在哪、有什么）

| 产物 | 内容 |
|---|---|
| stdout 单行 JSON（实时 flush） | 阶段事件：导入、初始化、任务准备、每次请求 `model_request_start/end`（耗时、finish_reason、**usage**）、bash 元数据、脱敏异常链 |
| `<output>/.arc_adapter/calls.jsonl` | 每次 HTTP 尝试一行：耗时、finish_reason、完整 usage，或失败异常链 |
| `<output>/.arc_adapter/payloads.jsonl` | append-only 增量日志（`prefix_len` + `append`），按序折叠可重建每次尝试发出的**字节级** payload（`scripts/reconstruct_payload.py`）；图片以 sha256 占位符剥离，可由 `.arc_requirements/` 原图确定性还原 |
| `<output>/.arc_adapter/trajectory.json` | 上游完整消息历史：每个 prompt（含 base64 图片）、每个响应 dump（含 reasoning 思考原文）、每条 bash 命令与输出；每步落盘，run 结束后图片同样剥离 |
| `<output>/.arc_adapter/run_summary.json` | status、llm_calls（逻辑调用）、model_http_attempts、token 聚合（成功尝试）、错误链 |

失败请求没有 usage（服务端未返回），token 聚合只覆盖成功尝试；`actual_api_cost_usd` 恒为 `null`（端点定价未知）。

## 上传

`python scripts/package_agent.py` → `dist/mini_swe_arcbench_adapter.zip`（源码白名单，无密钥/venv/node_modules）。以 Python agent 类型上传，表单 Base URL/Key 映射到 `OPENAI_BASE_URL`/`OPENAI_API_KEY`。

## 测试（不花 API）

```bash
python -m pip install pytest
python -m pytest -q   # 本地回环 HTTP mock：CLI 成功/断连、calls.jsonl、payload 快照、脱敏
```

## 安全

上游 bash 工具会在输出目录执行模型生成的任意 shell 命令；请在隔离的可信工作区运行。

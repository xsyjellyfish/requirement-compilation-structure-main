# ARC-Bench BookStack 验收环境（独立、只读官方用例）

这个目录只做一件事：用 ARC-Bench 官方书签需求包（requirements + Playwright 官方用例）去验收
Ver3 编译器生成的 BookStack 应用，并把每条失败归因到「生成应用 / 编译器通用规则 / 测试环境」。
它不修改编译器，不修改生成应用的 `package.json`，**也不修改任何官方断言**。

## 目录内容与来源

| 路径 | 说明 |
| --- | --- |
| `requirements/` | 官方 `arc-bench/bookstack/requirements/`，含 `requirements.yaml` 与 `reference/` |
| `tests/` | 官方 `arc-bench/bookstack/tests/`，含 `helpers.ts`（35 个用例 / 24 个 spec） |
| `setup-playwright.cjs` / `.ps1` / `.sh` | 官方三个 setup 脚本，逐字节复制 |
| `official-manifest.json` | 每个官方文件的相对路径、字节数、SHA-256、git blob SHA，以及上游 commit |
| `scripts/` | 运行、校验、归因、编译辅助脚本 |
| `playwright.config.ts` | 由官方 `setup-playwright.ps1` 生成，**不要手改** |

上游快照：`e59696bbc68578c3b3da1ad47f9a0ac07e581184`（2026-11-03）。
`requirements.yaml` 的 SHA-256 记录在每份分析报告的「环境事实」表里。

## 一次性准备

```powershell
cd bookstack-tests
powershell -File ./setup-playwright.ps1            # 生成 config、装依赖、装 Chromium
powershell -File ./scripts/verify-official-tests.ps1   # 确认官方文件未被改动
```

## 常规流程

```powershell
# 1. 只看用例清单（不启动应用）
npm run test:e2e:acceptance -- -List

# 2. 生成被测应用（默认只打印命令；加 -ConfirmRun 才真正跑 13 轮编译）
powershell -File ./scripts/generate-bookstack-app.ps1
powershell -File ./scripts/generate-bookstack-app.ps1 -ConfirmRun

# 3. 跑全部 35 个用例；-AppDir 会让脚本自动安装/构建/初始化/启动/关闭应用
powershell -File ./scripts/run-bookstack-tests.ps1 `
  -AppDir ../outputs/bookstack -AppUrl http://127.0.0.1:3000

# 4. 单条用例 / 关键字
powershell -File ./scripts/run-bookstack-tests.ps1 -AppUrl http://127.0.0.1:3000 -Spec REQ-4.3
powershell -File ./scripts/run-bookstack-tests.ps1 -AppUrl http://127.0.0.1:3000 -Grep 'Favorite'

# 5. 失败归因 + 打开报告
npm run test:e2e:analyze
npm run test:e2e:report
```

## 产物位置

| 产物 | 路径 |
| --- | --- |
| HTML 报告 | `playwright-report/index.html` |
| Playwright 原始机器结果 | `reports/results-<时间戳>.json` |
| 本次运行记录（URL、策略、退出码） | `reports/run-<时间戳>.json` |
| 失败截图 / trace / video / error-context | `test-results/<用例名>-chromium/` |
| 失败分析报告 | `reports/failure-analysis-<时间戳>.md` |
| 应用启动日志（`-AppDir` 时） | `reports/app-<时间戳>.log` |

`reports/`、`test-results/`、`playwright-report/` 已在 `.gitignore` 中。

## 失败归因口径

`scripts/analyze-bookstack-failures.ps1` 只按证据签名分类，绝不通过放宽断言来「修复」失败：

| 特征 | 结论 |
| --- | --- |
| `ERR_CONNECTION_REFUSED` / `ERR_EMPTY_RESPONSE` | C：应用未启动或 URL 不对（环境） |
| `E2E_API_CONTRACT_ERROR` / `Unknown API route` | B：编译器通用生成规则（前端调用与已接受契约不一致） |
| 定位器正好是 `Benchmark Bootstrap Shelf` / `BookStack User` / `bookstack_user@example.com` | A：requirements 声明的初始化数据没生成 |
| 等待 `getByRole/getByLabel/getByText` 超时 | A：生成应用缺区域 / 控件 / 可访问名称 |
| `toHaveValue/toHaveText/toHaveURL/toHaveCount` 不符 | A：生成应用行为或状态错误 |
| 只超时、无其它线索 | 未分类：必须人工看 trace 与 video |

## 退出码

- `run-bookstack-tests.ps1` 直接返回 Playwright 的退出码（0 = 全绿）。
- `verify-official-tests.ps1` 检测到官方用例/需求被增删改即返回 1，并把差异逐条打印。
- `generate-bookstack-app.ps1` 不加 `-ConfirmRun` 时永远是 0（没有产生任何模型调用）。

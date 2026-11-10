"""Mini6 -> Dify Testing Workflow bridge (generation smoke test + log analysis).

Reads the *current* requirements.yaml; never embeds business-specific test cases.
Uses Dify streaming API to avoid long blocking HTTP waits.
Only writes generated Playwright code. It does NOT execute model-produced code.

Usage:
  python mini6_dify_client.py path/to/requirements.yaml
  python mini6_dify_client.py path/to/requirements.yaml --context context.txt
  python mini6_dify_client.py path/to/requirements.yaml --mode analyze --log playwright.log

Set DIFY_API_KEY in environment or a .env next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib import error, request
from http.client import RemoteDisconnected
from socket import timeout as SocketTimeout


def _read_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding exported environment variables."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"DIFY_API_KEY", "DIFY_API_BASE_URL"}:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _text_file(path: Path | None) -> str:
    return path.read_text(encoding="utf-8-sig") if path else ""


def call_dify(payload: dict, api_key: str, base_url: str) -> dict:
    """Call a Dify Workflow via SSE (no long silent blocking request)."""
    url = base_url.rstrip("/") + "/workflows/run"
    payload = dict(payload)
    payload["response_mode"] = "streaming"
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        },
        method="POST",
    )
    print("正在连接 Dify (streaming)...", flush=True)
    finished = None
    seen = 0
    try:
        with request.urlopen(req, timeout=180) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if not data_text or data_text == "[DONE]":
                    continue
                try:
                    event_obj = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                event_type = event_obj.get("event", "")
                if event_type == "workflow_started":
                    print("Dify 工作流已启动", flush=True)
                elif event_type == "node_finished":
                    seen += 1
                    info = event_obj.get("data") or {}
                    title = info.get("title") or info.get("node_id") or "node"
                    status = info.get("status", "")
                    print(f"节点完成 #{seen}: {title} {status}", flush=True)
                elif event_type in {"error", "workflow_failed"}:
                    msg = event_obj.get("message") or (event_obj.get("data") or {}).get("error") or event_obj
                    raise RuntimeError(f"Dify streaming error: {msg}")
                elif event_type == "workflow_finished":
                    finished = event_obj.get("data")
                    print("Dify 工作流执行结束", flush=True)
                    break
    except error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Dify HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"无法连接 Dify: {exc.reason}") from exc
    except (RemoteDisconnected, ConnectionResetError, BrokenPipeError, SocketTimeout, TimeoutError) as exc:
        raise RuntimeError(
            "Dify 连接被中断；请检查 Dify 日志和本机网络/代理，"
            "并尝试用较小的需求输入验证。"
        ) from exc

    if not isinstance(finished, dict):
        raise RuntimeError(
            "连接结束，但没有收到 workflow_finished 事件；"
            "请检查 Dify 日志、反向代理和请求超时设置。"
        )
    if finished.get("status") != "succeeded":
        raise RuntimeError(f"Dify 工作流失败: {finished.get('error') or finished.get('status')}")
    result = (finished.get("outputs") or {}).get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Dify 输出 result 不是合法 JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Dify data.outputs.result 缺少 JSON 对象")
    if result.get("ok") is not True:
        raise RuntimeError("Dify 校验失败: " + json.dumps(result, ensure_ascii=False)[:2000])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path, help="Path to requirements.yaml or its directory")
    parser.add_argument("--context", type=Path, help="Optional plain-text code/routes summary")
    parser.add_argument("--visual-context", type=Path, help="Optional text describing reference images")
    parser.add_argument("--mode", choices=["generate", "analyze"], default="generate")
    parser.add_argument("--log", type=Path, help="Playwright output log (required for analyze)")
    parser.add_argument("--out", type=Path, default=Path("dify_testing_output"))
    args = parser.parse_args()

    _read_env_file(Path(__file__).resolve().parent / ".env")
    api_key = os.getenv("DIFY_API_KEY", "").strip()
    if not api_key:
        parser.error("DIFY_API_KEY is missing; add it to environment or .env next to this script")
    base_url = os.getenv("DIFY_API_BASE_URL", "https://api.dify.ai/v1").strip()
    if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1") and not base_url.startswith("http://localhost"):
        parser.error("Use HTTPS for Dify API (HTTP only for localhost)")
    req_file = args.requirements / "requirements.yaml" if args.requirements.is_dir() else args.requirements
    if not req_file.is_file():
        parser.error(f"Cannot find requirements: {req_file}")
    if args.mode == "analyze" and (args.log is None or not args.log.is_file()):
        parser.error("--mode analyze requires an existing --log file")

    requirements = req_file.read_text(encoding="utf-8-sig")
    context = _text_file(args.context)
    visual_context = _text_file(args.visual_context)
    log = _text_file(args.log)
    for name, value, maximum in (
        ("requirements", requirements, 100000),
        ("code_context", context, 100000),
        ("visual_context", visual_context, 40000),
        ("test_result", log, 100000),
    ):
        if len(value) > maximum:
            parser.error(f"{name} exceeds Dify input limit ({len(value)} > {maximum}); split by requirement first")

    response = call_dify(
        {
            "inputs": {
                "mode": args.mode,
                "requirements": requirements,
                "code_context": context,
                "visual_context": visual_context,
                "test_result": log,
            },
            "response_mode": "blocking",
            "user": "mini6-local-testing",
        },
        api_key,
        base_url,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    result_path = args.out / ("generation.json" if args.mode == "generate" else "analysis.json")
    result_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.mode == "generate":
        cases = response.get("cases", [])
        generated = response.get("playwright_test", "")
        if not isinstance(cases, list) or not cases or not isinstance(generated, str) or not generated.strip():
            raise RuntimeError("No executable tests generated; inspect " + str(result_path))
        spec_path = args.out / "generated.spec.ts"
        spec_path.write_text(generated, encoding="utf-8")
        print(f"Generation OK: {len(cases)} cases")
        print(f"Test file: {spec_path.resolve()}")
        print(f"JSON report: {result_path.resolve()}")
        if "test.skip" in generated:
            print("WARNING: generated tests include test.skip; missing fixtures can reduce coverage")
        print("Tests are NOT executed yet. Review them before running in an isolated environment.")
    else:
        print("Failure analysis:", response.get("overall_status", "unknown"))
        print("JSON report:", result_path.resolve())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

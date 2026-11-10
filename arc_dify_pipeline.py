"""Optional, requirement-driven Dify testing stage for ARC-Bench mini6.

The Coding Agent remains the original mini-SWE-agent. Dify is used only if an
application API key is securely injected at runtime. No business tests are
hardcoded. A missing key never breaks the original application submission.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import urlopen

BASE = Path(__file__).resolve().parent


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    val = int(raw)
    if not minimum <= val <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return val


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_logged(cmd: list[str], cwd: Path, timeout: int, path: Path,
                env: dict[str, str] | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="replace") as stream:
        try:
            result = subprocess.run(cmd, cwd=cwd, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=timeout, check=False)
            return result.returncode
        except subprocess.TimeoutExpired:
            stream.write(f"\n[INTEGRATION] timeout after {timeout}s\n")
            return 124


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_app(project: Path, path: Path, deadline: float) -> tuple[subprocess.Popen, str, Any]:
    port = _available_port()
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    # A generated app must not inherit model/Dify authentication credentials.
    for key in list(env):
        if key.upper().endswith("_API_KEY") or key.upper() in {"OPENAI_BASE_URL", "DIFY_API_BASE_URL"}:
            env.pop(key, None)
    env["PORT"] = str(port)
    stream = path.open("w", encoding="utf-8", errors="replace")
    args = ["bash", "deploy.sh"]
    opts = {"cwd": project, "env": env, "stdout": stream, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        opts["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        opts["start_new_session"] = True
    proc = None
    try:
        proc = subprocess.Popen(args, **opts)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stream.flush()
                raise RuntimeError(f"deploy.sh exited with {proc.returncode}; inspect {path}")
            try:
                with urlopen(url + "/", timeout=2) as resp:
                    if 200 <= resp.status < 500:
                        return proc, url, stream
            except HTTPError as exc:
                if 400 <= exc.code < 500:
                    return proc, url, stream
            except Exception:
                pass
            time.sleep(2)
        raise TimeoutError(f"Application did not start before deadline; inspect {path}")
    except Exception:
        _stop_app(proc, stream)
        raise


def _stop_app(proc: subprocess.Popen | None, stream: Any) -> None:
    if proc is not None and proc.poll() is None:
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=8)
        except Exception:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
    if stream is not None:
        stream.close()


def _write_selected_requirements(source: Path, limit: int, destination: Path) -> None:
    """Preserve the selected source scenarios for focused failure analysis."""
    import yaml
    from mini6_dify_batch import iter_units, make_payload

    original = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    entries = [yaml.safe_load(make_payload(node, scenario, ancestors))
               for node, scenario, ancestors in list(iter_units(original))[:limit]]
    destination.write_text(yaml.safe_dump({"selected_requirements": entries},
                                               allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_code_context(project: Path, destination: Path) -> None:
    """Offer routes/labels as locator hints; never send .env or private logs."""
    import re
    lines = ["The following file paths and UI hints come from the implementation,",
             "not from the requirements. Do not treat them as expected behavior."]
    patterns = (r'(?:href|to|role|aria-label|data-testid|placeholder)\s*=\s*[\"\'][^\"\']{1,100}[\"\']',
                r'\b(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*[\"\'][^\"\']{1,100}[\"\']')
    for branch in ("frontend/src", "backend/src"):
        directory = project / branch
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.suffix not in {".tsx", ".ts", ".jsx", ".js"} or not path.is_file():
                continue
            lines.append("File: " + path.relative_to(project).as_posix())
            data = path.read_text(encoding="utf-8", errors="replace")[:15000]
            hints = []
            for pattern in patterns:
                hints.extend(re.findall(pattern, data))
            lines.extend("  " + hint[:110] for hint in hints[:15])
            if sum(map(len, lines)) > 11000:
                break
    destination.write_text("\n".join(lines)[:12000], encoding="utf-8")


def run_post_generation(
    *,
    requirement_path: Path,
    output_dir: Path,
    deadline: float,
    repair_callback: Callable[[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run *optional* Dify generation, Playwright, failure analysis and limited repair.

    Never changes the original Coding Agent result / platform submission status.
    For safety, generated TypeScript should only execute in an isolated disposable
    assessment container. The separate test directory is not a security boundary.
    """
    mode = os.getenv("ARC_DIFY_MODE", "auto").lower().strip()
    if mode not in {"auto", "off", "required"}:
        raise ValueError("ARC_DIFY_MODE must be auto, off or required")
    root = output_dir / ".arc_adapter" / "dify_testing"
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"status": "skipped", "mode": mode, "attempts": []}
    summary_path = root / "integration_summary.json"

    def save(message: str) -> dict[str, Any]:
        result["status"] = message
        _write_json(summary_path, result)
        print(f"[DIFY-INTEGRATION] status={message}", flush=True)
        return result

    if mode == "off":
        return save("skipped_disabled")
    if not os.environ.get("DIFY_API_KEY", "").strip():
        if mode == "required":
            return save("missing_key_required")
        # ARC-Bench does not automatically inherit the user's local .env.
        return save("skipped_no_dify_api_key_original_agent_preserved")
    if time.monotonic() + 90 > deadline:
        return save("skipped_insufficient_time")
    if not (output_dir / "deploy.sh").is_file():
        return save("skipped_missing_deploy_sh")

    source = requirement_path / "requirements.yaml" if requirement_path.is_dir() else requirement_path
    limit = _int_env("ARC_DIFY_LIMIT", 1, 1, 100)
    rounds = _int_env("ARC_DIFY_REPAIR_ROUNDS", 0, 0, 3)
    generated = root / "generated"
    selected_requirements = root / "selected_requirements.yaml"
    _write_selected_requirements(source, limit, selected_requirements)
    code_context = root / "code_context.txt"
    _write_code_context(output_dir, code_context)
    batch = [sys.executable, "-u", str(BASE / "mini6_dify_batch.py"), str(source),
             "--limit", str(limit), "--out", str(generated),
             "--context", str(code_context)]
    result["selected_units_limit"] = limit
    result["repair_rounds_limit"] = rounds
    remaining = max(1, int(deadline - time.monotonic()))
    print(f"[DIFY-INTEGRATION] generating tests for up to {limit} requirement/scenario units", flush=True)
    gen_rc = _run_logged(batch, BASE, min(remaining, 210), root / "generation.log")
    result["generation_return_code"] = gen_rc
    if gen_rc != 0:
        return save("generation_failed_original_app_preserved")
    specs = sorted((generated / "tests").glob("*.spec.ts"))
    result["generated_files"] = len(specs)
    if not specs:
        return save("generation_empty_original_app_preserved")

    for attempt in range(rounds + 1):
        if time.monotonic() + 100 > deadline:
            return save("partial_time_budget_exhausted")
        proc, stream = None, None
        detail = {"attempt": attempt + 1, "status": "unknown"}
        result["attempts"].append(detail)
        try:
            app_deadline = min(deadline - 90, time.monotonic() + 180)
            proc, url, stream = _start_app(output_dir, root / f"app_{attempt+1}.log", app_deadline)
            detail["local_app_url"] = url
            test_command = [sys.executable, "-u", str(BASE / "mini6_dify_run_tests.py"),
                            "--run", "--spec", str(generated / "tests"),
                            "--url", url, "--out", str(root / f"run_{attempt+1}"),
                            "--analyze", "--requirements", str(selected_requirements)]
            if os.getenv("ARC_DIFY_SKIP_BROWSER_INSTALL", "0") == "1":
                test_command.append("--skip-browser-install")
            remaining = max(1, int(deadline - time.monotonic()))
            rc = _run_logged(test_command, BASE, min(remaining, 480),
                             root / f"runner_{attempt+1}.log")
            detail["runner_return_code"] = rc
            all_summaries = sorted((root / f"run_{attempt+1}").glob("*/summary.json"))
            if all_summaries:
                latest = all_summaries[-1]
                report = json.loads(latest.read_text(encoding="utf-8"))
                detail["status"] = report.get("status", "unknown")
                detail["counts"] = report.get("counts", {})
                detail["summary_path"] = str(latest)
                analysis_file = latest.parent / "dify_analysis.json"
                if analysis_file.exists():
                    detail["analysis_path"] = str(analysis_file)
            else:
                detail["status"] = "runner_error"
            if detail["status"] == "PASSED":
                return save("tests_passed")
        except Exception as exc:
            detail["status"] = "environment_error"
            detail["error"] = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            _stop_app(proc, stream)
            _write_json(summary_path, result)

        if attempt >= rounds or repair_callback is None:
            break
        if time.monotonic() + 180 > deadline:
            break
        # Evidence is untrusted. It is a diagnostic hint, not an authoritative spec.
        evidence = {"test_result": detail}
        latest = Path(detail.get("summary_path", "/nonexistent"))
        if latest.is_file():
            analysis = latest.parent / "dify_analysis.json"
            if analysis.is_file():
                report = json.loads(analysis.read_text(encoding="utf-8"))
                evidence["failure_analysis"] = {
                    "failures": report.get("failures", [])[:8],
                    "next_actions": report.get("next_actions", [])[:8],
                }
        feedback = json.dumps(evidence, ensure_ascii=False)[:12000]
        repair_budget = min(180, max(1, int(deadline - time.monotonic()) - 90))
        repair_result = repair_callback(feedback, repair_budget)
        detail["repair_status"] = repair_result.get("status", "unknown")
    return save("tests_incomplete_original_app_preserved")

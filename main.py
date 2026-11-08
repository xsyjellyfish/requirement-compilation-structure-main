"""Compile a requirement directory into an executable application."""

import argparse
from pathlib import Path
import os
import sys

from infrastructure.compiler import Compiler
from infrastructure.reporting import final, status as run
from infrastructure.utils.cli import resolve_directories
from infrastructure.utils.presentation.logging import configure_console

DEPLOY_NOTE = ("Required checks failed and were explicitly accepted, so the application is still "
    "handed over. Exit 0 keeps it deployable for the ARC-Bench Runner; use --exit-policy strict "
    "for CI.")


def policy_from_environment() -> str:
    """Read ARC_EXIT_POLICY, accepting only a known policy so a typo cannot hide failures."""
    value = os.environ.get(run.POLICY_ENVIRONMENT, "").strip().lower()
    return value if value in run.POLICIES else run.DEFAULT_POLICY


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read public options and validate paths and the backend port before compilation."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("requirement_path", type=Path,
        help="Requirement directory containing requirements.yaml and reference assets")
    parser.add_argument("-o", "--output-dir", type=Path, required=True,
        help="Directory for the generated application and .arc artifacts")
    parser.add_argument("--model-log", "--debug-model-io", dest="model_log", action="store_true",
        help="Show detailed model I/O and save per-call prompt/response logs; usage and timing are always shown")
    parser.add_argument("--port", type=int, default=3000,
        help="Default generated backend port and frontend API proxy target (default: 3000)")
    parser.add_argument("--exit-policy", choices=run.POLICIES, default=policy_from_environment(),
        help=("Exit-status contract: 'strict' (default) exits 3 when a required check or "
            "requirement failed; 'deploy' exits 0 for a deployable application only when "
            f"{run.ACCEPT_FLAG} also accepted the failures, and the report keeps them "
            f"(env: {run.POLICY_ENVIRONMENT})"))
    parser.add_argument(run.ACCEPT_FLAG, dest="accept_known_failures", action="store_true",
        help=("Deliver a run whose final checks failed. Without this explicit opt-in a "
            "failed final check always exits non-zero, whatever the exit policy"))
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        args.requirement_path, args.output_dir = resolve_directories(
            argparse.Namespace(input=args.requirement_path, output=args.output_dir))
        if not args.requirement_path.is_dir():
            raise ValueError(f"Input directory does not exist: {args.requirement_path}")
        if not (args.requirement_path / "requirements.yaml").is_file():
            raise ValueError(f"Missing requirement document: {args.requirement_path / 'requirements.yaml'}")
        if args.output_dir.exists() and not args.output_dir.is_dir():
            raise ValueError(f"Output must be a directory: {args.output_dir}")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return args



def summaries(report: dict) -> str:
    """Name every failed check, requirement, API, page or trigger for the terminal."""
    summary = report.get("summary") or {}
    checks, tests = summary.get("checks") or {}, summary.get("tests") or {}
    requirements = summary.get("requirements") or {}
    lines = ["Checks: " + str(checks.get("total", 0)) + " recorded; failed: "
        + (", ".join(checks.get("failed") or []) or "none"),
        f"Tests: passed {tests.get('passed', 0)}, failed {tests.get('failed', 0)}, "
        f"skipped {tests.get('skipped', 0)}, unverified passes {tests.get('unverified', 0)}",
        f"Requirements: {requirements.get('green', 0)} green, {requirements.get('failed', 0)} failed, "
        f"{requirements.get('unknown', 0)} unknown, "
        f"{requirements.get('not_applicable', 0)} not applicable"]
    for entry in report.get("failures") or []:
        body = " ".join((entry.get("reason") or "").split())[:300]
        lines.append(f"  - [{entry.get('kind')}] {entry.get('id')} in {entry.get('stage')} "
            f"({entry.get('status')}): {body}")
    return "\n".join(lines)


def finish(compiler, policy: str, accepted: bool = False) -> int:
    """Complete the report, record the exit decision in it, and return the process exit code.

    The final assembly is also the delivery gate: it is the only call that applies
    ``gate``, so the report states a ``delivery`` verdict (CLEAR / ACCEPTED / BLOCKED) and
    the failed checks that produced it. It does not rewrite the run status -- a failed final
    check is not a crash -- and the exit contract turns that verdict into the exit code:
    a partial run stays COMPLETED_WITH_FAILURES and exits 0 only when the operator passed
    ``--accept-known-failures`` under the deploy policy.
    """
    output_dir, arc_dir = getattr(compiler, "output_dir", None), getattr(compiler, "arc_dir", None)
    report = compiler.report if isinstance(getattr(compiler, "report", None), dict) else {}
    status, deployable = report.get("status"), False
    try:
        final.build_report(report, output_dir=output_dir, arc_dir=arc_dir,
            gate=True, accepted=accepted)
        status, deployable = report.get("status"), bool(report.get("deployable"))
        code = run.exit_code(status, policy, deployable, accepted)
        report["exit"] = {"code": code, "policy": policy, "accepted_failures": bool(accepted),
            "reason": run.exit_reason(status, policy, deployable, accepted)}
        final.write_report(arc_dir, report)
    except (OSError, TypeError, ValueError) as error:
        print(f"Final report unavailable ({error}); compiler status: {status}", file=sys.stderr)
        code = run.exit_code(status, policy, deployable, accepted)
    return code


def main(argv: list[str] | None = None) -> None:
    configure_console()
    os.environ.setdefault("MODEL", "deepseek-flash")
    args = parse_args(argv)
    accepted = bool(args.accept_known_failures)
    compiler = Compiler(args.requirement_path, args.output_dir, model_log=args.model_log, port=args.port)
    try:
        compiler.compile()
    except KeyboardInterrupt:
        print(f"Compilation interrupted at {compiler.current_stage or 'startup'}.", file=sys.stderr)
        finish(compiler, args.exit_policy, accepted)
        raise SystemExit(run.EXIT_INTERRUPTED)
    except Exception as error:
        print(f"Compilation failed at {compiler.current_stage or 'startup'}: {error}", file=sys.stderr)
        finish(compiler, args.exit_policy, accepted)
        raise SystemExit(run.EXIT_FATAL) from error
    code = finish(compiler, args.exit_policy, accepted)
    report = compiler.report if isinstance(compiler.report, dict) else {}
    status = report.get("status") if isinstance(report.get("status"), str) else None
    report_path = compiler.arc_dir / final.REPORT
    if code == run.EXIT_GREEN and status == run.GREEN:
        print(f"All required checks passed. Report: {report_path}")
        return
    if code == run.EXIT_GREEN:
        if status == run.COMPLETED_WITH_FAILURES:
            print(f"Status {status}. {DEPLOY_NOTE}\n{summaries(report)}\nReport: {report_path}",
                file=sys.stderr)
        return
    print(f"Status {status} (exit {code}). {(report.get('exit') or {}).get('reason', '')}\n"
        f"{summaries(report)}\nReport: {report_path}", file=sys.stderr)
    raise SystemExit(code)



if __name__ == "__main__":
    main()

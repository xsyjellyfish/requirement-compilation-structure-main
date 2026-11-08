"""Step 13: run the independent black-box acceptance layer against the generated application.

Step 12 already runs this layer at the end of its own acceptance, so a full pipeline never
skips it. This pass exists to run *only* the layer: after a manual change, after a repair, or
when reviewing an application that was generated earlier. It derives the journeys again from
the saved IR, audits them, and runs them in a real browser against a temporary database.

    python 13_accept_frontend_blackbox.py --input input --output outputs/demo
"""

from contextlib import ExitStack
from pathlib import Path

from infrastructure.shared.models.text import TextModel
from infrastructure.reporting.status import is_passed
from infrastructure.stages.frontend.acceptance.checks import AcceptanceChecks
from infrastructure.stages.frontend.acceptance.pipeline import REPORT, run_acceptance
from infrastructure.stages.frontend.triggers.context import load_e2e_context
from infrastructure.stages.frontend.ui.runtime import prepare_frontend
from infrastructure.shared.project.dependencies import install_dependencies
from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.logging import console_log


def accept_frontend_blackbox(input_dir: Path, arc_dir: Path, pause: bool = False,
        max_turns: int = 3, offline: bool = False) -> dict:
    """Derive, audit and run the black-box journeys, repairing the application when a step fails."""
    if max_turns < 0:
        raise ValueError("--max-turns must not be negative")
    tree, schema, backend, ui, interactions, images, contexts = load_e2e_context(arc_dir, input_dir)
    output = arc_dir.parent.resolve()
    if not ui["pages"]:
        raise ValueError("No UI was designed; run 09_design_ui.py and 10_implement_ui.py first")
    prepare_frontend(output)
    install_dependencies(output)
    checks = AcceptanceChecks(output)
    checks.install_browser()
    model = TextModel(json_mode=False) if max_turns and not offline else None
    report = run_acceptance(arc_dir, tree, schema, backend, ui, interactions, checks,
        DemoConsole(pause), model=model, max_turns=max_turns)
    findings = [*(report.get("derivation_problems") or []), *(report.get("problems") or []),
        *(report.get("unverified") or [])]
    print(f"Black-box acceptance {report['status']}: {report.get('tests') or report.get('audit')}\n"
        + ("" if not findings else "\n".join(findings) + "\n")
        + f"Report: {arc_dir / REPORT}", flush=True)
    return report


def main() -> None:
    parser = create_parser("Step 13: Black-box Acceptance Journeys")
    parser.add_argument("--max-turns", type=int, default=3,
        help="Maximum application repair rounds when a journey fails (0 disables repair)")
    parser.add_argument("--offline", action="store_true",
        help="Run and audit the journeys without a model call; failures are reported, not repaired")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            input_dir, output_dir = resolve_directories(args)
            stack.enter_context(console_log(output_dir, debug_model_io=args.debug_model_io))
            result = accept_frontend_blackbox(input_dir, output_dir / ".arc", args.pause,
                args.max_turns, args.offline)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")

    if not is_passed(result["status"]):
        if result["status"] == "BLOCKED_UPSTREAM":
            # The journeys cannot be derived from the published IR, so nothing about the
            # application was verified: only the design pass that owns the IR can remove the cause.
            parser.exit(1, "The journeys cannot be derived from the published UI IR, so the"
                " application was not verified. Repair the design pass (09_design_ui.py) first.\n"
                + "\n".join(result.get("derivation_problems") or []) + "\n")
        parser.exit(1, "The black-box acceptance journeys did not pass. See the .arc report.\n")


if __name__ == "__main__":
    main()

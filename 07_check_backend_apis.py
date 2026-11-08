"""Step 07: statically verify every backend API contract, then display the design.

The pass is a real check: an interface the later generators cannot use stops the
pipeline here with an API ID, the failing field, the reason and a correction, rather
than surfacing as broken TypeScript in step 08.
"""

from contextlib import ExitStack
import json
from pathlib import Path

from infrastructure.stages.backend.context import load_context
from infrastructure.stages.backend.contracts import (
    format_contract_summary, format_traceability_summary, validate_contracts)
from infrastructure.stages.backend.primitives import validate_design
from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.utils.files import read_artifact
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.logging import console_log


def check_backend_apis(arc_dir: Path, pause: bool = False) -> dict:
    """Verify methods, paths, parameters, identities, references, schemas and obligations."""
    tree, _, obligations = load_context(arc_dir)
    design = read_artifact(arc_dir, "backend_apis.json")
    console = DemoConsole(pause)
    # Interface contract first: it names the exact field for the design model to repair.
    validate_contracts(design, tree=tree)
    # The obligation gate is unchanged; this pass now re-checks it on the saved artifact.
    validate_design(design, obligations, tree=tree)
    console.show("Step 07 | API contract checks", format_contract_summary(design))
    console.show("Step 07 | Requirement <-> API traceability", format_traceability_summary(design))
    console.show("Step 07 | Backend API design", json.dumps(design, ensure_ascii=False, indent=2))
    return design


def main() -> None:
    parser = create_parser("Step 07: Verify Backend APIs")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            _, output_dir = resolve_directories(args)
            stack.enter_context(console_log(output_dir, debug_model_io=args.debug_model_io))
            check_backend_apis(output_dir / ".arc", args.pause)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")


if __name__ == "__main__":
    main()

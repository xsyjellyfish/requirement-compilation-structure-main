"""Measure the JSON payload stage 12 sends per requirement, without any model call.

Two selection policies are measured against the same generated project:

``legacy``  the pre-optimization policy: every editable frontend/backend source is
            supplied as writable and both source trees are supplied again as references.
``focused`` the optimized policy from the production helpers: only the requirement's
            triggers, its pages, their local imports and the handlers of the APIs it
            calls are supplied; everything else arrives after a failure names it.

Usage:
    python scripts/audit_prompt_payload.py output/demo2 --mode legacy
    python scripts/audit_prompt_payload.py output/demo2 --mode both --json out.json

Only characters are measured; the token column is the character count divided by
``CHARS_PER_TOKEN`` and is labelled as an estimate everywhere it appears.
"""

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infrastructure.shared.agent.context import SourceFiles  # noqa: E402
from infrastructure.shared.agent.files import editable_sources, source_paths  # noqa: E402
from infrastructure.shared.project.entrypoints import selected_path  # noqa: E402
from infrastructure.stages.frontend.triggers.context import load_e2e_context  # noqa: E402
from infrastructure.stages.frontend.triggers.design import trigger_obligations  # noqa: E402
from infrastructure.stages.frontend.triggers.e2e_runtime import FIXTURE  # noqa: E402
from infrastructure.stages.frontend.triggers.implementation import scenario_context  # noqa: E402
from infrastructure.stages.frontend.triggers.scope import model_context  # noqa: E402
from infrastructure.stages.frontend.triggers.testing import test_path  # noqa: E402

# A code/JSON mix needs roughly 3.6 characters per token. The column is an estimate.
CHARS_PER_TOKEN = 3.6

FAILURE = {"e2e": {"returncode": 1, "passed": 0, "failed": 1, "skipped": 0, "flaky": 0,
    "failures": ["Error: expect(received).toBeVisible()\n\nlocator resolved to 2 elements\n"
        "  at REQ.spec.ts:42:7"], "output": ""},
    "contract": {"returncode": 0, "problems": []},
    "typecheck": {"returncode": 0, "output": "compiled"}}


def trigger_paths(output, trigger_ids):
    """Resolve the same target paths stage 12 reserves, without writing the index."""
    paths = {}
    for tid in sorted(trigger_ids):
        relative = selected_path(output, "trigger", tid)
        if relative is None:
            relative = "frontend/src/triggers/trigger-" + sha256(tid.encode()).hexdigest()[:16] + ".ts"
        paths[tid] = relative
    return paths


def legacy_test_workspace(selection, output, rid, relative, ui=None, backend=None, context=None):
    """Verbatim copy of the pre-optimization test-generation selection in stage 12."""
    return selection.workspace(target=("e2e_test", rid), existing=relative,
        references=["frontend/src/ui-runtime.ts", FIXTURE, "backend/tests/support/context.ts",
            *source_paths(output, "frontend/src"), *source_paths(output, "backend/src")])


def legacy_implementation_workspace(selection, backend_selection, output, context_tids, relative,
        ui=None, backend=None, context=None):
    """Verbatim copy of the pre-optimization trigger-implementation selection in stage 12."""
    workspace = selection.workspace(triggers=context_tids,
        paths=[*editable_sources(output, "frontend/src"), "frontend/src/style.css"],
        references=[relative, "backend/tests/support/context.ts",
            *source_paths(output, "frontend/src"), *source_paths(output, "backend/src")])
    backend_selection.include(workspace, editable_sources(output, "backend/src"))
    return workspace


def focused_test_workspace(selection, output, rid, relative, ui, backend, context):
    """Production test-generation selection."""
    from infrastructure.stages.frontend.triggers.workspaces import test_workspace
    return test_workspace(selection, output, context, rid, relative)


def focused_implementation_workspace(selection, backend_selection, output, context_tids, relative,
        ui, backend, context):
    """Production trigger-implementation selection."""
    from infrastructure.stages.frontend.triggers.workspaces import implementation_workspace
    return implementation_workspace(selection, backend_selection, output, context, context_tids, relative)


def payload_size(task, workspace, feedback=()):
    """Serialize the request exactly as the agent loop does and count its characters."""
    files = workspace.state()
    body = json.dumps({"task": task, "files": files, "feedback": list(feedback)},
        ensure_ascii=False, sort_keys=True)
    content = sum(len(value["content"] or "") for value in files.values())
    return {"files": len(files), "writable": sum(1 for value in files.values() if value["writable"]),
        "content_chars": content,
        "task_chars": len(json.dumps(task, ensure_ascii=False, sort_keys=True)),
        "feedback_chars": len(json.dumps(list(feedback), ensure_ascii=False, sort_keys=True)),
        "payload_chars": len(body),
        "estimate_tokens": round(len(body) / CHARS_PER_TOKEN)}


def measure(output: Path, input_dir: Path, mode: str) -> dict:
    """Build both agent requests for every requirement and record their size."""
    arc = output / ".arc"
    tree, schema, backend, ui, design, _images, _contexts = load_e2e_context(arc, input_dir)
    selection = SourceFiles(output, ui)
    backend_selection = SourceFiles(output, root="backend/src", tests_root="backend/tests")
    build_test = legacy_test_workspace if mode == "legacy" else focused_test_workspace
    build_impl = legacy_implementation_workspace if mode == "legacy" else focused_implementation_workspace
    rows, implemented = [], set()
    for rid in tree.leaves:
        context = scenario_context(ui, design, rid)
        trigger_ids = set(design["requirements"][rid])
        context_tids = set(context["triggers"])
        if not context["page_subtrees"] and not trigger_ids:
            continue
        called = {action["api"] for trigger in context["triggers"].values()
            for action in trigger["action"] if action["kind"] == "api"}
        context["obligations"] = {tid: trigger_obligations(trigger, backend)
            for tid, trigger in context["triggers"].items()}
        context["backend_apis"] = {aid: backend["apis"][aid] for aid in sorted(called)}
        relative = test_path(rid, output)
        generation = build_test(selection, output, rid, relative, ui, backend, context)
        # Mirrors the real stage: the E2E spec never opens the database, so the entity
        # schema is not task context; the path is only sent when the suite already exists.
        test_task = {**model_context(context),
            "test_path": relative if (output / relative).is_file() else None, "max_tests": 2}
        row = {"requirement": rid, "api_ids": sorted(called),
            "test_round": payload_size(test_task, generation)}
        workspace = build_impl(selection, backend_selection, output, context_tids, relative,
            ui, backend, context)
        task = {**model_context(context),
            "implementation_paths": trigger_paths(output, context_tids)}
        row["impl_round"] = payload_size(task, workspace)
        selection.feedback(workspace, backend_selection.feedback(workspace, FAILURE))
        row["impl_repair_round"] = payload_size(task, workspace)
        implemented.update(trigger_ids | (context_tids - implemented))
        rows.append(row)
    return {"mode": mode, "output": str(output), "requirements": rows}


def report(result: dict) -> None:
    """Print one row per requirement and the stage totals."""
    phases = ("test_round", "impl_round", "impl_repair_round")
    print("mode: " + result["mode"])
    print("{:12s} {:18s} {:>5s} {:>8s} {:>11s} {:>11s} {:>13s} {:>9s}".format(
        "requirement", "phase", "files", "writable", "source chars", "task chars", "payload chars", "~tokens"))
    total = 0
    for row in result["requirements"]:
        for phase in phases:
            size = row[phase]
            total += size["payload_chars"]
            print("{:12s} {:18s} {:5d} {:8d} {:11,d} {:11,d} {:13,d} {:9,d}".format(row["requirement"],
                phase, size["files"], size["writable"], size["content_chars"], size["task_chars"],
                size["payload_chars"], size["estimate_tokens"]))
    print("{:12s} {:18s} {:5s} {:8s} {:11s} {:11s} {:13,d} {:9,d}".format("total", "all phases", "", "",
        "", "", total, round(total / CHARS_PER_TOKEN)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Generated project directory containing .arc")
    parser.add_argument("--input", type=Path, default=ROOT / "input", help="Requirement input directory")
    parser.add_argument("--mode", choices=("legacy", "focused", "both"), default="both")
    parser.add_argument("--json", type=Path, help="Write the measurements to this JSON file")
    args = parser.parse_args(argv)
    modes = ("legacy", "focused") if args.mode == "both" else (args.mode,)
    results = [measure(args.output.resolve(), args.input.resolve(), mode) for mode in modes]
    for result in results:
        report(result)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

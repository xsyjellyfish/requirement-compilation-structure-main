"""Isolate failed implementation tasks and complete independent final checks."""

from contextlib import contextmanager
import re
from uuid import uuid4

from infrastructure.reporting.status import FAILED, GREEN, run_status
from infrastructure.shared.agent.diagnosis import classify
from infrastructure.shared.agent.files import EXCLUDED
from infrastructure.utils.files import save_bytes, save_json
from infrastructure.utils.presentation.logging import model_usage


def _snapshot(output):
    paths = {path.relative_to(output).as_posix()
        for root in ("backend/src", "backend/tests", "frontend/src", "frontend/tests")
        for path in (output / root).rglob("*") if path.is_file() and not path.is_symlink()
        and not any(part in EXCLUDED or part.startswith(".") for part in path.relative_to(output).parts)}
    index = ".arc/implementation_paths.json"
    if (output / index).is_file():
        paths.add(index)
    return {path: (output / path).read_bytes() for path in paths}


@contextmanager
def implementation_task(output, entry, console, label):
    """Archive rejected files, restore the task baseline, and continue on acceptance failure.

    Missing prerequisites are checked before this boundary. Disk errors, interrupts,
    and unexpected compiler exceptions propagate; they are not business failures. A task
    whose failure names an upstream artifact is recorded as ``BLOCKED_UPSTREAM`` and
    propagates too, because only the pass that owns that artifact can remove the cause.
    """
    before = _snapshot(output)
    try:
        yield
    except ValueError as error:
        after = _snapshot(output)
        changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        name = re.sub(r"[^\w.-]+", "_", label).strip("._")[:100] or "task"
        directory = output / ".arc/failures" / f"{name}.{uuid4().hex[:8]}"
        for path in sorted(changed):
            if path in after:
                save_bytes(directory / "files" / path, after[path])
        diagnosis = classify(getattr(error, "diagnosis", None) or error)
        upstream = getattr(error, "upstream", None) or {}
        blocked = bool(upstream)
        failure = {"status": "BLOCKED_UPSTREAM" if blocked else "FAILED", "task": label,
            "phase": entry.get("status"),
            "error": str(error), "turns": getattr(error, "turns", 0),
            "feedback": getattr(error, "feedback", None), "changed_files": sorted(changed),
            "deleted_files": sorted(changed - after.keys()),
            # A blocked task is caused by a file this pass may not write, so the record names
            # the blocker's cause; ``classify`` only sees the message and cannot know that.
            "category": upstream.get("category") or diagnosis["category"],
            "upstream_stage": upstream.get("stage"), "stalled": bool(getattr(error, "stalled", False)),
            "budget": getattr(error, "spent", None), "blocked": blocked,
            "category_reason": upstream.get("reason") or diagnosis["reason"],
            "archive": directory.relative_to(output).as_posix()}
        save_json(directory / "failure.json", failure)
        for path in sorted(changed):
            if path in before:
                save_bytes(output / path, before[path])
            else:
                (output / path).unlink(missing_ok=True)
        entry.update(failure)
        console.show(label + " | " + failure["status"] + "; continuing",
            str(error) + "\nBaseline restored. Failure archive: " + failure["archive"])
        if blocked:
            # The cause is a file this task may not write. Isolating the task would report a
            # failure that no retry can remove, so the owning pass gets the request instead.
            raise


def final_checks(console, **checks):
    """Attempt every final compiler/test/build check even if an earlier check fails."""
    results = {}
    for name, (action, accept) in checks.items():
        result = None
        try:
            result = action()
            if result.get("returncode"):
                raise ValueError(result.get("output", f"{name} failed"))
            if accept:
                accept(result)
            results[name] = {"status": GREEN, "result": result}
        except (ValueError, RuntimeError) as error:
            results[name] = {"status": FAILED, "result": result, "error": str(error)}
        console.show("Final verification | " + name + " | " + results[name]["status"],
            results[name].get("error") or (result or {}).get("output", "Passed"))
    return results


def failed_checks(results: dict) -> dict[str, str]:
    """Name every final check that did not pass, with the evidence behind it."""
    return {name: str((check or {}).get("error") or "check did not pass")
        for name, check in (results or {}).items() if (check or {}).get("status") != GREEN}


def final_verification(console, repair=None, **checks):
    """Run every final check, then gate delivery on the result.

    The checks are recorded even when one fails, so the report keeps the full picture. A
    failed check is a *gate*, not a record: ``repair`` is offered one focused round with
    the failing evidence and the complete results, and every check is rerun afterwards so
    only the state of the sources decides the outcome. When no repair is available, or it
    did not clear the failures, the results stay failed and the caller must not deliver.
    """
    results = final_checks(console, **checks)
    failed = failed_checks(results)
    if not failed or repair is None:
        return results
    console.show("Final verification | repair round", "Failed: " + ", ".join(sorted(failed)))
    try:
        repair(failed, results)
    except (ValueError, RuntimeError) as error:
        # A repair that could not be applied leaves the original, honest result in place.
        console.show("Final verification | repair unavailable", str(error))
        return results
    rerun = final_checks(console, **checks)
    remaining = failed_checks(rerun)
    console.show("Final verification | after repair",
        "Passed all final checks" if not remaining else "Still failing: " + ", ".join(sorted(remaining)))
    return rerun


def complete_report(report, entries, verification):
    """Separate completion of the process from successful acceptance.

    A task with an explicit FAILED status and a final check that did not pass are both
    failures of the run; a status that is missing or unrecognized is never GREEN. The failed
    check is recorded in ``blocked_checks`` so the stage, the report and the exit contract
    all see which check blocks delivery, while the stage keeps the honest
    COMPLETED_WITH_FAILURES verdict: the pass did finish, its acceptance did not hold. The
    stage's recorded model usage is stored with the report, so the tokens, reasoning time
    and latency of every requirement, page and API stay auditable after the run.
    """
    report["failures"] = [dict(entry) for entry in entries if entry.get("status") == FAILED]
    report["verification"] = verification
    report["blocked_checks"] = sorted(failed_checks(verification))
    usage = model_usage()
    if usage:
        report["model_usage"] = usage
    report["status"] = run_status([entry.get("status") for entry in entries]
        + [check.get("status") for check in verification.values()])
    return report

"""Assemble the machine-readable and human-readable report of one compilation run.

``.arc/compilation.json`` is the authoritative record. Every stage entry keeps:

    status / fatal / recoverable   how far the pass got and whether the run can continue
    checks                         each final check's status, honest counts and reason
    items                          each requirement, API, page, table or trigger status
    report / log                   where the full check output and log lines live

``.arc/final_report.md`` renders the same data for a human reader. Neither file is
produced by dropping failing tests: a failed check keeps its failures, its passing
tests become ``unverified``, and every failure is indexed with a reason and a log
location so a failed Requirement, API, page or Trigger can be located directly.
"""

from datetime import datetime, timezone
from pathlib import Path

from infrastructure.reporting.accounting import check_records, clip, reason, totals
from infrastructure.reporting.status import (COMPLETED_WITH_FAILURES, FAILED, GREEN, INTERRUPTED,
    NOT_APPLICABLE, PASS_STATUSES, UNKNOWN, classify, run_status)
from infrastructure.utils.files import save_json

#: Artifact each pass writes, relative to ``.arc``; None means the pass writes sources only.
ARTIFACTS = {
    "00_project_initialization": None,
    "01_explore_requirement": "requirements.json",
    "02_build_db_schema": "db_schema.json",
    "03_infer_db_obligations": "db_obligations.json",
    "04_check_db_obligations": "db_obligations.json",
    "05_implement_db": None,
    "06_design_backend_apis": "backend_apis.json",
    "07_check_backend_apis": "backend_apis.json",
    "08_implement_backend_tdd": "backend_tdd.json",
    "09_design_ui": "ui_design.json",
    "10_implement_ui": "ui_checks.json",
    "11_design_frontend_triggers": "frontend_triggers.json",
    "12_implement_frontend_tdd": "frontend_e2e.json",
    "13_accept_frontend_blackbox": "frontend_acceptance.json",
}

#: Stage report collections that carry a per-record status, and their failure-index kind.
COLLECTIONS = (("requirements", "requirement"), ("apis", "api"), ("pages", "page"),
    ("triggers", "trigger"), ("tables", "table"), ("entities", "entity"))

REPORT = "compilation.json"
DIGEST = "final_report.md"
LOG = "compiler.log"
TERMINAL = (GREEN, COMPLETED_WITH_FAILURES, FAILED, INTERRUPTED)


def utc_now() -> str:
    """Return a sortable UTC timestamp for the report."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def duration_seconds(started, finished) -> float | None:
    """Measure an interval in seconds when both of its timestamps are known."""
    try:
        return round((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds(), 2)
    except (TypeError, ValueError):
        return None


def _status_records(collection, kind: str) -> dict:
    """Collect the entries of one stage collection that declare an explicit status."""
    records = {}
    if not isinstance(collection, dict):
        return records
    for identifier, entry in collection.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), str):
            continue
        records[identifier] = {"kind": kind, "status": entry["status"], "reason": reason(entry),
            "archive": entry.get("archive")}
    return records


def item_records(result) -> dict:
    """Collect every requirement, API, page, table, entity, trigger or HTTP flow status."""
    result = result if isinstance(result, dict) else {}
    items = {}
    for key, kind in COLLECTIONS:
        items.update(_status_records(result.get(key), kind))
    http = result.get("http")
    if isinstance(http, dict):
        for collection, kind in (("apis", "api"), ("flows", "flow")):
            for identifier, entry in _status_records(http.get(collection), kind).items():
                items.setdefault(identifier, entry)
    return items


def declared_requirements(result) -> list[str]:
    """Read the requirement ids the requirement pass accepted, including nested children."""
    identifiers, pending = [], [result] if isinstance(result, dict) else []
    while pending:
        node = pending.pop(0)
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("id"), str):
            identifiers.append(node["id"])
        pending.extend(node.get("children") or [])
    return identifiers


def deployment(output_dir) -> dict:
    """Record the deployment evidence the ARC-Bench Runner relies on."""
    output_dir = Path(output_dir)
    return {"script": "deploy.sh" if (output_dir / "deploy.sh").is_file() else None,
        "manifest": "package.json" if (output_dir / "package.json").is_file() else None,
        "frontend_build": (output_dir / "frontend" / "dist").is_dir(),
        "backend_build": (output_dir / "backend" / "dist").is_dir()}


def stage_record(module: str, result, *, status: str | None = None, error=None, fatal: bool = False,
        started: str | None = None, log: dict | None = None) -> dict:
    """Describe one pass: how it ended, what it verified, and where its evidence lives."""
    result = result if isinstance(result, dict) else {}
    finished = utc_now()
    if status is None:
        status = result.get("status") if isinstance(result.get("status"), str) else GREEN
    record = {"status": status, "fatal": bool(fatal), "recoverable": not fatal,
        "started_at": started, "finished_at": finished,
        "duration_seconds": duration_seconds(started, finished),
        "report": ARTIFACTS.get(module), "log": log,
        "checks": check_records(result.get("verification")),
        "items": item_records(result),
        "failures": task_failures(result),
        "declared_requirements": declared_requirements(result) if module.startswith("01") else []}
    if error is not None:
        record["error_type"] = type(error).__name__
        record["error"] = clip(str(error))
    record["reason"] = clip(str(error)) if error is not None else _stage_reason(record)
    return record


def task_failures(result) -> list[dict]:
    """Keep the task-level failure records with bounded text; the full copy stays in the stage report."""
    entries = result.get("failures")
    if not isinstance(entries, list):
        return []
    return [{key: (clip(value) if isinstance(value, str) else value) for key, value in entry.items()}
        for entry in entries if isinstance(entry, dict)]


def _stage_reason(record: dict) -> str:
    """Name the checks and items that keep a pass out of GREEN."""
    failed = [identifier for identifier, entry in (record.get("items") or {}).items()
        if classify(entry["status"]) != "passed"]
    failed += [name for name, check in (record.get("checks") or {}).items() if check["status"] != GREEN]
    return ("Failed: " + ", ".join(failed)) if failed else ""


def requirement_status(sources: dict) -> str:
    """Combine the passes that reported one requirement without ever defaulting to GREEN."""
    statuses = list(sources.values())
    if not statuses:
        return UNKNOWN
    if any(classify(status) == "failed" for status in statuses):
        return next(status for status in statuses if classify(status) == "failed")
    if any(classify(status) == "unknown" for status in statuses):
        return UNKNOWN
    return GREEN if GREEN in statuses else NOT_APPLICABLE


def failure_index(stages: dict) -> list[dict]:
    """Index every failure with its kind, id, reason, stage report and log location."""
    index = []
    for module, record in stages.items():
        if record.get("fatal"):
            index.append({"stage": module, "kind": "fatal", "id": module, "status": record["status"],
                "reason": record.get("error") or "fatal error", "report": record.get("report"),
                "log": record.get("log")})
        for identifier, entry in sorted((record.get("items") or {}).items()):
            if classify(entry["status"]) == "passed":
                continue
            index.append({"stage": module, "kind": entry["kind"], "id": identifier,
                "status": entry["status"], "reason": entry["reason"], "archive": entry.get("archive"),
                "report": record.get("report"), "log": record.get("log")})
        for name, check in sorted((record.get("checks") or {}).items()):
            if check["status"] == GREEN:
                continue
            index.append({"stage": module, "kind": "check", "id": name, "status": check["status"],
                "reason": check["reason"], "counts": check["accepted"], "report": record.get("report"),
                "log": record.get("log")})
    return index


def _summary(stages: dict, requirements: dict, checks: list[dict]) -> dict:
    """Total what was verified, what failed, and what remains unknown, without merging them."""
    counted = totals([check["accepted"] for check in checks])
    failed_checks = [check["name"] for check in checks if check["status"] != GREEN]
    statuses = [requirement["status"] for requirement in requirements.values()]
    return {
        "stages": len(stages),
        "stages_green": sum(1 for record in stages.values() if classify(record["status"]) == "passed"),
        "stages_incomplete": [module for module, record in stages.items()
            if classify(record["status"]) != "passed"],
        "checks": {"total": len(checks), "green": len(checks) - len(failed_checks),
            "failed": failed_checks},
        "tests": {"total": counted["total"], "passed": counted["passed"], "failed": counted["failed"],
            "skipped": counted["skipped"], "flaky": counted["flaky"],
            "unverified": counted["unverified"]},
        "requirements": {"total": len(requirements), "green": statuses.count(GREEN),
            "not_applicable": sum(1 for status in statuses if status in PASS_STATUSES and status != GREEN),
            "failed": sum(1 for status in statuses if classify(status) == "failed"),
            "unknown": sum(1 for status in statuses if classify(status) == "unknown")},
    }



def build_report(report: dict, *, output_dir, arc_dir, gate: bool = False,
        accepted: bool = False) -> dict:
    """Complete an in-progress run report in place with statuses, totals, failures and paths.

    ``gate`` marks the final assembly: it records the delivery decision as ``delivery`` so
    the report states whether the run may be handed over, and why. It deliberately does not
    rewrite the run status -- a failed final check is not a crash -- because the status keeps
    its own meaning (COMPLETED_WITH_FAILURES) while the exit contract refuses to deliver it
    without ``accepted``. The in-progress snapshots the compiler writes after every pass keep
    ``gate`` off, so a check a later pass fixes never leaves a permanent delivery verdict
    behind.
    """
    stages = report.get("stages") or {}
    status = report.get("status")
    declared, sources = [], {}
    for module, record in stages.items():
        declared.extend(record.get("declared_requirements") or [])
        for identifier, entry in (record.get("items") or {}).items():
            if entry.get("kind") == "requirement":
                sources.setdefault(identifier, {})[module] = entry["status"]
    requirements = {identifier: {"status": requirement_status(sources.get(identifier) or {}),
        "sources": sources.get(identifier) or {}}
        for identifier in sorted(set(declared) | set(sources))}
    checks = [check for record in stages.values() for check in (record.get("checks") or {}).values()]
    failed_checks = sorted({name for record in stages.values()
        for name, check in (record.get("checks") or {}).items() if check.get("status") != GREEN})
    if status not in (FAILED, INTERRUPTED):
        # A requirement no pass reported stays UNKNOWN, and UNKNOWN is never GREEN, so an
        # unverified requirement keeps the whole run out of GREEN as well.
        status = run_status([record.get("status") for record in stages.values()]
            + [entry["status"] for entry in requirements.values()])
    evidence = deployment(output_dir)
    finished = utc_now()
    report.update({
        "status": status,
        "finished_at": finished,
        "duration_seconds": duration_seconds(report.get("started_at"), finished),
        "application": str(Path(output_dir)),
        "deployable": bool(evidence["script"] and evidence["manifest"]),
        "deploy": evidence,
        "failed_checks": failed_checks,
        "accepted_failures": bool(accepted),
        "summary": _summary(stages, requirements, checks),
        "requirements": requirements,
        "failures": failure_index(stages),
        "reports": {name: ".arc/" + name for name in
            dict.fromkeys([REPORT, DIGEST, *(value for value in ARTIFACTS.values() if value)])},
        "logs": {name: name for name in (LOG, "model_calls.jsonl")},
    })
    if gate:
        # Only the terminal assembly decides delivery, and only an explicit acceptance may
        # let a failed final check through: a partial run is never delivered by default.
        report["delivery"] = ({"status": "ACCEPTED" if accepted else "BLOCKED",
            "failed_checks": failed_checks, "accepted": bool(accepted),
            "application": report["deployable"]}
            if failed_checks else {"status": "CLEAR", "failed_checks": [], "accepted": bool(accepted),
                "application": report["deployable"]})
    return report


def write_report(arc_dir, report: dict) -> None:
    """Write the machine-readable report and its human-readable digest."""
    arc_dir = Path(arc_dir)
    arc_dir.mkdir(parents=True, exist_ok=True)
    save_json(arc_dir / REPORT, report)
    (arc_dir / DIGEST).write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: dict) -> str:
    """Render the report as a short digest that names every failure and where to read it."""
    summary = report.get("summary") or {}
    tests, checks = summary.get("tests") or {}, summary.get("checks") or {}
    requirements = summary.get("requirements") or {}
    stages = report.get("stages") or {}
    incomplete = summary.get("stages_incomplete") or []
    lines = [f"# Compilation report: {report.get('status')}", "",
        f"- Application: `{report.get('application')}` (deployable: {report.get('deployable')})",
        f"- Passes attempted: {len(stages)} (not GREEN: {', '.join(incomplete) or 'none'})",
        f"- Checks: {checks.get('total')} recorded, failed: {', '.join(checks.get('failed') or []) or 'none'}",
        f"- Failed final checks: {', '.join(report.get('failed_checks') or []) or 'none'}"
        + (" (explicitly accepted by the operator)" if report.get("accepted_failures") else
            " (a failed final check blocks delivery)"),
        f"- Tests: passed {tests.get('passed')}, failed {tests.get('failed')}, "
        f"skipped {tests.get('skipped')}, unverified passes {tests.get('unverified')}",
        f"- Requirements: {requirements.get('green')} green, {requirements.get('failed')} failed, "
        f"{requirements.get('unknown')} unknown, "
        f"{requirements.get('not_applicable')} not applicable",
        "", "## Passes", "",
        "| Pass | Status | Failed checks | Tests passed / failed | Duration | Report |",
        "| --- | --- | --- | --- | --- | --- |"]
    for module, record in stages.items():
        report_checks = record.get("checks") or {}
        failed = [name for name, check in report_checks.items() if check["status"] != GREEN]
        passed = sum(check["accepted"]["passed"] for check in report_checks.values())
        broken = sum(check["accepted"]["failed"] for check in report_checks.values())
        duration = record.get("duration_seconds")
        lines.append(f"| {module} | {record.get('status')} | {', '.join(failed) or '—'} | "
            f"{passed} / {broken} | {'—' if duration is None else str(duration) + 's'} | "
            f"{'.arc/' + record['report'] if record.get('report') else '—'} |")
    rejected = [(identifier, entry) for identifier, entry in (report.get("requirements") or {}).items()
        if classify(entry.get("status")) != "passed"]
    lines += ["", "## Requirements that are not accepted"]
    lines += [f"- {identifier}: {entry.get('status')} ({', '.join(sorted(entry.get('sources') or {}))})"
        for identifier, entry in rejected] or ["- none"]
    lines += ["", "## Failures"]
    failures = report.get("failures") or []
    for entry in failures:
        location = entry.get("log") or {}
        where = (f"compiler.log lines {location.get('start_line')}-{location.get('end_line')}"
            if location.get("start_line") else "compiler.log")
        report_path = f"; report .arc/{entry['report']}" if entry.get("report") else ""
        archive = f"; evidence {entry['archive']}" if entry.get("archive") else ""
        body = " ".join((entry.get("reason") or "").split())[:400]
        lines.append(f"- [{entry.get('kind')}] {entry.get('id')} in {entry.get('stage')} "
            f"({entry.get('status')}): {body} ({where}{report_path}{archive})")
    lines += [""] if failures else ["- none", ""]
    lines += ["## Machine-readable report", "",
        f"`{REPORT}` records every pass, check, requirement, API, page and trigger status above; "
        "each entry points at the stage report and at the compiler.log line range that holds the "
        "full output."]
    return "\n".join(lines) + "\n"


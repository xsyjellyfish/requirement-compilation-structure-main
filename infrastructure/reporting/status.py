"""Run status vocabulary and the CLI exit-code contract.

One compilation ends in exactly one of four states:

    GREEN                     every required check and every requirement passed.
    COMPLETED_WITH_FAILURES   every pass was attempted and a deployable application
                              exists, but at least one check or requirement did not
                              pass. Recoverable: the failure was isolated and the
                              remaining passes still ran.
    FAILED                    a fatal error stopped the pipeline, so the application
                              cannot be completed or deployed. Not recoverable.
    INTERRUPTED               the operator interrupted the run (Ctrl-C).

The state is recorded in ``.arc/compilation.json``; it is not inferred from a partial
count of tests and it never defaults to GREEN. A status that is missing, unknown, or
still in progress is classified as "unknown", which is never accepted.

Exit codes are a *separate* contract, because the ARC-Bench agent Runner deploys the
application directory that ``deploy.sh`` builds. Two policies exist:

    strict (default)  GREEN -> 0, COMPLETED_WITH_FAILURES -> 3, FAILED -> 1
    deploy            GREEN -> 0, COMPLETED_WITH_FAILURES -> 0 only when the application
                      is still deployable *and* the operator explicitly accepted the
                      known failures (``--accept-known-failures``); the report keeps
                      COMPLETED_WITH_FAILURES either way

A failed final check is a gate, not a record: ``COMPLETED_WITH_FAILURES`` never exits 0
on its own, so a run whose final verification saw a failure cannot be handed over
without a deliberate opt-in. An interrupt is always 130 and a fatal error is always 1,
whatever the policy: nothing is deployable after a crash. ``--exit-policy`` (or
``ARC_EXIT_POLICY``) and ``--accept-known-failures`` are the only knobs, so the Bench
Runner cannot silently turn a failed run into a delivered one by editing the pipeline.
"""

GREEN = "GREEN"
COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"
NOT_STARTED = "NOT_STARTED"
RUNNING = "RUNNING"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

#: Explicit statuses that accept a check, page, API, trigger or requirement. A parent
#: requirement that owns no API or trigger of its own is accepted, but not as "verified".
PASS_STATUSES = frozenset({GREEN, NOT_APPLICABLE, "NO_UI", "NO_APIS", "NO_DIRECT_APIS",
    "NO_TRIGGERS", "NO_DIRECT_TRIGGERS", "SKIPPED"})
#: Statuses that mean "this item or check did not pass"; anything else is unknown. The
#: in-progress statuses are listed on purpose: a pass interrupted mid-item leaves one.
#: ``BLOCKED_UPSTREAM`` marks work that a later pass could not finish because an earlier
#: pass published an artifact it may not rewrite; it is a failure of the run until the
#: upstream repair succeeds, never a pass. ``UNVERIFIED`` is a layer that could not derive
#: or run its checks at all: it verified nothing, so it may not count as a pass either.
FAILURE_STATUSES = frozenset({FAILED, COMPLETED_WITH_FAILURES, "REJECT", "REPAIR",
    "REPAIR_REQUIRED", "RED_CONFIRMED", "IMPLEMENTING", "IMPORTING", "BLOCKED_UPSTREAM",
    "UNVERIFIED"})

TERMINAL_STATUSES = (GREEN, COMPLETED_WITH_FAILURES, FAILED, INTERRUPTED)

POLICY_ENVIRONMENT = "ARC_EXIT_POLICY"
STRICT = "strict"
DEPLOY = "deploy"
POLICIES = (STRICT, DEPLOY)
DEFAULT_POLICY = STRICT
#: The only way to deliver a run whose final checks failed: an explicit CLI opt-in.
ACCEPT_FLAG = "--accept-known-failures"

EXIT_GREEN = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 3
EXIT_INTERRUPTED = 130
#: 2 stays reserved for argparse usage errors; 4..127 stay free for future policies.


def is_passed(status) -> bool:
    """Accept only an explicit passing status; a missing status is never a pass."""
    return isinstance(status, str) and status in PASS_STATUSES


def classify(status) -> str:
    """Classify one item or check status as passed, failed, or unknown."""
    if is_passed(status):
        return "passed"
    if not isinstance(status, str):
        return "unknown"
    return "failed" if status in FAILURE_STATUSES else "unknown"


def run_status(statuses) -> str:
    """Combine stage statuses into the run status.

    A stage that reports FAILED without raising is a recoverable, isolated failure: the
    pipeline still attempted the remaining passes, so the run is COMPLETED_WITH_FAILURES.
    Only an exception marks the run itself FAILED.
    """
    statuses = list(statuses)
    if INTERRUPTED in statuses:
        return INTERRUPTED
    if not statuses:
        return NOT_STARTED
    return GREEN if all(classify(status) == "passed" for status in statuses) else COMPLETED_WITH_FAILURES


def exit_code(status, policy: str = DEFAULT_POLICY, deployable: bool = False,
        accepted: bool = False) -> int:
    """Map a run status and an exit policy to the process exit code.

    Only a terminal verdict decides a non-zero code. ``None``, a non-string, or an
    in-progress status means there is no verdict to judge (a replaced compiler, or a run
    that never produced a report), which is reported as 0; a real run always ends with
    GREEN, COMPLETED_WITH_FAILURES, FAILED or INTERRUPTED.

    ``accepted`` is the operator's explicit ``--accept-known-failures``; without it even
    the deploy policy keeps a partial run out of exit 0, so no policy can deliver a run
    whose final checks failed by accident.
    """
    if status == INTERRUPTED:
        return EXIT_INTERRUPTED
    if status == FAILED:
        return EXIT_FATAL
    if status != COMPLETED_WITH_FAILURES:
        return EXIT_GREEN
    # A partial result is never silently successful, whatever the policy.
    return EXIT_GREEN if policy == DEPLOY and deployable and accepted else EXIT_PARTIAL


def exit_reason(status, policy: str = DEFAULT_POLICY, deployable: bool = False,
        accepted: bool = False) -> str:
    """Explain the exit code in one line for the terminal and for the report."""
    code = exit_code(status, policy, deployable, accepted)
    application = "deployable application kept" if deployable else "no deployable application"
    if code == EXIT_GREEN and status == GREEN:
        return "All required checks and requirements passed."
    if code == EXIT_GREEN:
        return (f"Status {status}: required checks failed, but the {application} and "
            f"{ACCEPT_FLAG} explicitly accepted them; '{DEPLOY}' policy exits 0.")
    if code == EXIT_FATAL:
        return f"Status {status}: a fatal error stopped the pipeline ({application})."
    if code == EXIT_INTERRUPTED:
        return f"Status {status}: the run was interrupted ({application})."
    if policy == DEPLOY and deployable:
        return (f"Status {status}: required checks failed ({application}); delivery needs "
            f"{ACCEPT_FLAG} because a failed final check is never accepted implicitly.")
    return (f"Status {status}: required checks failed ({application}); "
        f"use '--exit-policy {DEPLOY} {ACCEPT_FLAG}' to exit 0 while still reporting the failures.")

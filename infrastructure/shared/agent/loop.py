"""Bounded JSON edit -> fixed acceptance check -> repair loop."""

import os
from time import perf_counter

from infrastructure.shared.agent.diagnosis import blocker, classify
from infrastructure.shared.agent.files import FileWorkspace
from infrastructure.shared.agent.protocol import FILE_EDIT_RULES
from infrastructure.shared.agent.proposals import RESPONSE_ERRORS, response_feedback
from infrastructure.utils.presentation.logging import model_tokens_used

# A repeated identical check failure is not progress. Two genuine attempts at the same
# signature are allowed; the third identical result stops the loop instead of repeating
# an ineffective repair until the round budget is empty.
STALL_LIMIT = 3

# A task that keeps changing its failure can still walk the whole round budget while it
# burns model time. These per-task ceilings are deliberately larger than any converging
# repair observed (the slowest accepted task used 17 calls and ~370 s of model time), so
# only a divergent loop reaches them. ``0`` disables one ceiling.
BUDGET_SECONDS = 1200.0
BUDGET_TOKENS = 600_000


class AgentExhausted(ValueError):
    """A recoverable task failure with the last real repair feedback."""

    def __init__(self, label, turns, feedback):
        super().__init__(f"Agent exhausted {turns} rounds for {label}")
        self.turns = turns
        self.feedback = feedback


class AgentStalled(AgentExhausted):
    """A loop stopped early because the same classified failure repeated without progress."""

    def __init__(self, label, turns, feedback, diagnosis, repeats):
        super().__init__(label, turns, feedback)
        self.diagnosis = diagnosis
        self.repeats = repeats
        self.stalled = True
        self.args = (f"Agent stopped after {turns} rounds for {label}: {diagnosis['category']} "
            f"failed {repeats} times with the same result ({diagnosis['reason'][:200]})",)


class AgentBudgetExceeded(AgentExhausted):
    """A loop stopped because its task spent the configured time or token budget."""

    def __init__(self, label, turns, feedback, spent):
        super().__init__(label, turns, feedback)
        self.spent = spent
        self.budget = True
        self.args = (f"Agent stopped after {turns} rounds for {label}: task budget exhausted "
            f"({', '.join(f'{key} {value:,}' for key, value in sorted(spent.items()))})",)


def repair_budget(seconds=None, tokens=None):
    """Return the per-task repair budget from explicit values or ARC_REPAIR_BUDGET_*.

    ``seconds`` covers the whole task including its acceptance checks; ``tokens`` is the
    reported prompt+completion total the task's own model calls added. Both default to the
    module ceilings and can be tightened or disabled (``0``) per run from the environment.
    """
    def value(explicit, name, default):
        """Resolve one ceiling: explicit argument, environment override, then default."""
        if explicit is not None:
            return float(explicit)
        text = os.environ.get(name, "").strip()
        try:
            return float(text) if text else float(default)
        except ValueError:
            raise ValueError(f"{name} must be a number of seconds or tokens") from None
    budget = {"seconds": value(seconds, "ARC_REPAIR_BUDGET_SECONDS", BUDGET_SECONDS),
        "tokens": int(value(tokens, "ARC_REPAIR_BUDGET_TOKENS", BUDGET_TOKENS))}
    return budget if budget["seconds"] > 0 or budget["tokens"] > 0 else None


def check_budget(budget, label, turns, started, feedback, tokens_used=0):
    """Stop before another model call when this task already exceeded its budget."""
    if not budget:
        return
    elapsed = perf_counter() - started
    spent = {}
    if budget["seconds"] > 0 and elapsed >= budget["seconds"]:
        spent["seconds"] = round(elapsed, 1)
    if budget["tokens"] > 0 and tokens_used >= budget["tokens"]:
        spent["tokens"] = int(tokens_used)
    if spent:
        raise AgentBudgetExceeded(label, turns, feedback, spent)


class AgentBlocked(AgentExhausted):
    """A loop stopped on the first round because the cause is not in the editable files.

    ``upstream`` is the blocker record: its ``stage`` owns the cause (or is ``None`` for a
    filesystem failure), and its ``evidence`` is the text the later pass must act on.
    """

    def __init__(self, label, turns, feedback, diagnosis, upstream):
        super().__init__(label, turns, feedback)
        self.diagnosis = diagnosis
        self.upstream = upstream
        self.blocked = True
        self.args = (f"Agent blocked after {turns} rounds for {label}: {upstream['category']} "
            f"cannot be repaired by editing the supplied files ({upstream['reason'][:200]})",)


def run_file_agent(model, instructions: str, task: dict, files: FileWorkspace, gate,
        max_turns: int, console, label: str, feedback=None, feedback_filter=None,
        stall_limit: int | None = STALL_LIMIT, budget=None) -> dict:
    """Refresh files and paths; optionally select failure-related files before repair.

    The stage-owned feedback filter may add writable sources/read-only references
    and condense check reports. It cannot change the acceptance gate or its result.
    A failure that repeats with one signature and no progress stops the loop early;
    it is reported as ``stalled`` with the classification and the last real feedback.
    A failure whose cause is a published artifact or the filesystem stops the loop on
    its first occurrence, reported as ``blocked`` with the pass that owns the cause.
    A task that exceeds its time/token budget stops before the next model call, so one
    divergent task cannot consume the run's whole model allowance.
    """
    if max_turns < 1:
        raise ValueError("--max-turns must be positive")
    if stall_limit is not None and stall_limit < 1:
        raise ValueError("stall_limit must be positive or None")
    budget = repair_budget() if budget is None else budget
    feedback = [] if feedback is None else [feedback_filter(feedback) if feedback_filter else feedback]
    previous, repeats = None, 0
    started = perf_counter()
    tokens_started = model_tokens_used()
    for turn in range(1, max_turns + 1):
        check_budget(budget, label, turn, started, feedback, model_tokens_used() - tokens_started)
        console.show(f"{label} | Edit round {turn}/{max_turns}",
            "Generate file edits, then run the stage's compilation and test acceptance checks.")
        response, applied, checked = None, False, False
        try:
            response = model.generate_json(instructions + "\n\n" + FILE_EDIT_RULES,
                {"task": files.task_state(task), "files": files.state(), "feedback": feedback})
            files.apply(response)
            applied = True
            if files.new_file is not None:
                raise ValueError("Create the target file in the first edit before running acceptance checks")
            console.show(f"{label} | Edit round {turn}/{max_turns}",
                str([edit["path"] for edit in response["edits"]]))
            checked = True
            # ``seconds`` makes a slow repair visible next to the rounds it took: a loop that
            # needs three rounds of a 90-second acceptance check costs more than its turn
            # count suggests, and only the elapsed time shows it. The final gate is included.
            result = gate()
            return {"turns": turn, "seconds": round(perf_counter() - started, 2), "checks": result}
        except (*RESPONSE_ERRORS, RuntimeError) as error:
            message = str(error)
            rejected = (getattr(error, "feedback", {"error": message}) if applied
                else response_feedback(error, response))
            diagnosis = classify(rejected if checked else message)
            console.show(f"{label} | Repair feedback | {diagnosis['category']}", message)
            feedback = [feedback_filter(rejected) if feedback_filter else rejected]
            # A cause outside the editable files is not a repair attempt: stop now, so the
            # stage can route it to the pass that owns the file instead of retrying it.
            cause = blocker(rejected) if checked else None
            if cause:
                console.show(f"{label} | BLOCKED_UPSTREAM | {cause['category']}",
                    f"{cause['reason']}\nNo supplied file can fix this; stopping instead of retrying.")
                raise AgentBlocked(label, turn, feedback, diagnosis, cause) from error
            repeats = repeats + 1 if checked and diagnosis["signature"] == previous else int(checked)
            previous = diagnosis["signature"] if checked else None
            if checked and stall_limit is not None and repeats >= stall_limit:
                console.show(f"{label} | STALLED | {diagnosis['category']}",
                    f"The same {diagnosis['category']} result repeated {repeats} times with no "
                    f"progress; stopping instead of retrying. {diagnosis['reason']}")
                raise AgentStalled(label, turn, feedback, diagnosis, repeats) from error
    raise AgentExhausted(label, max_turns, feedback)


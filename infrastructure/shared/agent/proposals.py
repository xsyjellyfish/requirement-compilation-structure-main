"""Retry model proposals when parsing or consuming their returned records fails."""

import json


RESPONSE_ERRORS = (ValueError, KeyError, TypeError, AttributeError, IndexError)
#: How many earlier rejections the repair request repeats, so one task cannot grow the
#: prompt without bound while the model still sees the failures it already produced.
ATTEMPT_HISTORY = 5
REPAIR_INSTRUCTIONS = """The previous proposal was rejected. Read correction.error and correction.details
in the input and fix that failure. correction.previous_response is the rejected
answer, not an accepted example. Return a complete replacement JSON object in the
original output format, including every required top-level section. Do not return
only the unchanged portion, a partial patch, or the correction metadata itself.
correction.previous_attempts lists the earlier rejections of this task; do not repeat
an answer that was already rejected for the same reason."""


class ResponseShapeError(ValueError):
    """Name the response path a proposal did not fill, and what it filled instead.

    A stage raises this before consuming a response, so a missing section is never
    replaced by an empty default. The repair loop forwards ``feedback`` to the next
    proposal, which is why it states the missing path, the expected structure, a
    complete example, the sections that were read, and the records that must survive.
    """

    def __init__(self, label, problem, *, recognized, expected, example, preserve, recoverable):
        self.problem = problem
        self.feedback = {"missing_field": problem["path"].split(".")[-1].split("[")[0],
            "expected_path": problem["path"], "expected": problem["expected"],
            "found": problem["found"], "fix": problem["fix"], "recognized": recognized,
            "example": example, "must_preserve": preserve, "recoverable": recoverable,
            "expected_structure": expected}
        super().__init__(
            f"{label} response is unusable at {problem['path']}: {problem['found']}. "
            f"{problem['expected']} is required there. {problem['fix']}")
        self.recognized, self.preserve, self.recoverable = recognized, preserve, recoverable

    def __str__(self):
        """Append what was already recognized, so the failure cannot hide it."""
        message = super().__str__()
        if self.recognized:
            message += " Recognized: " + json.dumps(self.recognized, ensure_ascii=False) + "."
        if self.preserve:
            message += " Keep: " + ", ".join(self.preserve) + "."
        return message


def response_feedback(error, response):
    """Describe an actual response-consumption failure, without adding design rules."""
    message = str(error)
    if isinstance(error, KeyError):
        key = error.args[0] if error.args else "(unknown)"
        message = f"Missing key {key!r} while reading the model response. Return the requested complete records."
    feedback = {"error": message, "error_type": type(error).__name__, "previous_response": response}
    if isinstance(error, json.JSONDecodeError):
        feedback["invalid_response"] = error.doc
    if hasattr(error, "feedback"):
        feedback["details"] = error.feedback
    return feedback


def attempt_summary(history):
    """Summarise earlier rejections so the next proposal can avoid repeating them."""
    return [{"attempt": item["attempt"], "error_type": item["error_type"], "error": item["error"][:240]}
        for item in history[-ATTEMPT_HISTORY:]]


def failure_diagnosis(feedback):
    """Render the last rejection as a diagnosable message instead of a bare key name."""
    message = feedback["error"]
    details = feedback.get("details")
    if isinstance(details, dict):
        diagnosis = {key: details[key] for key in ("expected_path", "recoverable") if key in details}
        if "recognized" in details:
            diagnosis["recognized"] = details["recognized"]
        if diagnosis:
            message += " Last diagnosis: " + json.dumps(diagnosis, ensure_ascii=False)
    return message


def propose_json(model, instructions, request, accept, console, label, max_attempts=10):
    """Generate and repair a proposal within a finite total model-call budget."""
    if max_attempts < 1:
        raise ValueError("--max-attempts must be positive")
    request = dict(request)
    history = []
    for attempt in range(1, max_attempts + 1):
        progress = f"{attempt}/{max_attempts}"
        console.show(f"{label} | Model proposal {progress}", "Generate the proposal; response-reading and obligation errors receive repair feedback.")
        response = None
        try:
            prompt = instructions if "correction" not in request else instructions + "\n\n" + REPAIR_INSTRUCTIONS
            if "correction" in request:
                correction = request["correction"]
                prompt += "\nActual failure: " + correction["error"]
                if correction.get("details"):
                    prompt += "\nRepair details:\n" + json.dumps(correction["details"], ensure_ascii=False, indent=2)
                if correction.get("previous_attempts"):
                    prompt += "\nEarlier rejections:\n" + json.dumps(correction["previous_attempts"], ensure_ascii=False, indent=2)
            response = model.generate_json(prompt, request)
            return accept(response), response
        except RESPONSE_ERRORS as error:
            feedback = response_feedback(error, response)
            feedback["failed_attempt"] = attempt
            if history:
                feedback["previous_attempts"] = attempt_summary(history)
            console.show(f"{label} | Repair feedback", json.dumps(feedback, ensure_ascii=False, indent=2))
            if attempt >= max_attempts:
                failure = RuntimeError(f"{label} exhausted {max_attempts} proposals: {failure_diagnosis(feedback)}")
                failure.feedback = feedback
                raise failure from error
            history.append({"attempt": attempt, "error_type": feedback["error_type"], "error": feedback["error"]})
            request["correction"] = feedback

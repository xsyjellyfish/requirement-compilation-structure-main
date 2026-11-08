"""Requirement-to-API database obligation coverage and proposal acceptance.

Obligation coverage stays the read/write gate it always was; the interface contract
rules live in ``infrastructure.stages.backend.contracts`` and run after coverage so a
proposal is accepted only when both the data effects and the interface are usable.

``read_api_proposal`` reads the two response sections before either gate runs, so a
section the model returned at another path is reported as that path and a missing
section is never replaced by an empty default. Coverage and contracts remain the only
places where an empty API set or an empty association is judged legal.
"""

from copy import deepcopy

from infrastructure.shared.agent.proposals import ResponseShapeError
from infrastructure.stages.backend.contracts import validate_contracts
from infrastructure.stages.database.obligations.rules import difference, union


#: The top-level sections one API proposal returns, and the order they are read in.
SECTIONS = ("apis", "requirement_apis")
#: One-level envelopes a JSON-mode model sometimes wraps its answer in.
ENVELOPES = ("response", "result", "data", "proposal", "output")
#: The structure every repair request quotes back when a required section is unusable.
STRUCTURE = {"apis": {"API-ID": "one complete definition"}, "requirement_apis": ["API-ID"]}
#: The smallest complete answer, so a repair cannot mistake the sections for a patch.
EXAMPLE = {
    "apis": {"API-list-notes": {"description": "List the persisted notes", "read": ["Note.text"],
        "write": [], "signature": {"name": "listNotes", "method": "GET", "path": "/api/notes"},
        "request": {"params": {"type": "object"}, "query": {"type": "object"},
            "body": {"type": "object"}, "headers": {"type": "object"}},
        "response": {"success": {"status": 200, "body": {"type": "object", "properties": {}},
            "headers": {"type": "object"}}, "errors": []},
        "precondition": [],
        "postcondition": [{"id": "Q-notes-listed", "description": "Returns every stored note", "status": 200}]}},
    "requirement_apis": ["API-list-notes"],
}


def shape_problem(path, expected, found, fix):
    """Describe one response-shape problem by the path a repair has to fill."""
    return {"path": path, "expected": expected, "found": found, "fix": fix}


def unusable(problem, label, recognized, preserve, recoverable=False):
    """Raise the repair-facing error for one unusable response path."""
    return ResponseShapeError(label, problem, recognized=recognized, expected=STRUCTURE,
        example=EXAMPLE, preserve=preserve, recoverable=recoverable)


def is_definition(value):
    """An API definition is an object carrying an interface, not a bare section value.

    ``{"apis": {"requirement_apis": ["API-1"]}}`` and the requirement-keyed variant both
    answer with a section where a definition belongs, so only this shape check decides
    whether the value is a definition or a section returned one level too deep.
    """
    return isinstance(value, dict) and ("signature" in value or "description" in value)


def proposal_payload(response, label):
    """Return the object that carries the proposal sections, accepting one envelope."""
    if not isinstance(response, dict):
        raise unusable(shape_problem("$", "a JSON object with the top-level sections "
            + " and ".join(SECTIONS), f"the response is {type(response).__name__}",
            "Return the complete object; an array or a bare string carries no API definitions."),
            label, {}, [])
    if not any(section in response for section in SECTIONS):
        for name in ENVELOPES:
            inner = response.get(name)
            if isinstance(inner, dict) and any(section in inner for section in SECTIONS):
                return inner
    return response


class ObligationCoverageError(ValueError):
    def __init__(self, rid, required, combined, gaps):
        self.feedback = {"requirement_id": rid, "required_obligations": required,
            "api_obligation_union": combined, **gaps}
        super().__init__(f"Obligation Propagation failed for {rid}: {self.feedback}")


def coverage(required, apis):
    combined = union(apis)
    return {"missing": difference(required, combined), "excess": difference(combined, required)}


def read_api_proposal(response, rid, known=()):
    """Return ``(proposal, notes)`` for one requirement, with path-level diagnostics.

    ``known`` are the API IDs earlier requirements already accepted, so an association
    may point at an existing API. Every rejected path names what was expected there,
    what was found, which sections were read, and which records the next answer must
    keep, because the repair request is the model's only view of this failure.
    """
    label = f"Backend API design {rid}"
    payload = proposal_payload(response, label)
    notes = []
    declared = payload.get("apis")
    if not isinstance(declared, dict):
        raise unusable(shape_problem("apis", "an object keyed by API ID",
            "missing" if "apis" not in payload else type(declared).__name__,
            'Return {"apis": {"API-ID": {...}}} as a top-level section of the same object that carries requirement_apis.'),
            label, {"top_level_keys": sorted(str(key) for key in payload)},
            [str(key) for key in sorted(payload, key=str) if key != "apis"])
    # A section returned inside apis is a misplaced section, never a definition: reading
    # it as one would invent an API named after the section.
    misplaced = {name: value for name, value in declared.items()
        if name in SECTIONS[1:] and not is_definition(value)}
    apis = {name: value for name, value in declared.items() if name not in misplaced}
    for name, value in misplaced.items():
        if name in payload and payload[name] is not None:
            notes.append(f"apis.{name} ignored: the top-level {name} section is the one read")
            continue
        payload = {**payload, name: value}
        notes.append(f"{name} was returned at apis.{name} and has been read from the top level")
    invalid = sorted((name for name, value in apis.items() if not isinstance(value, dict)), key=str)
    if invalid:
        raise unusable(shape_problem(f"apis.{invalid[0]}", "one complete API definition object",
            type(apis[invalid[0]]).__name__,
            "Keep one definition object per API ID; do not put a section name inside apis."),
            label, {"apis": sorted(apis, key=str)},
            [f"apis.{name}" for name in sorted(apis, key=str) if name not in invalid])
    listed = payload.get("requirement_apis", None)
    if isinstance(listed, dict):
        if rid not in listed:
            raise unusable(shape_problem(f"requirement_apis.{rid}", "an array of API IDs",
                f"keys {sorted(str(key) for key in listed)}",
                "List this requirement's API IDs under its own ID, or return one top-level array."),
                label, {"apis": sorted(apis, key=str)},
                [f"apis.{name}" for name in sorted(apis, key=str)])
        notes.append(f"requirement_apis was keyed by requirement and has been read at requirement_apis.{rid}")
        listed = listed[rid]
    if listed is None:
        raise unusable(shape_problem("requirement_apis", "a top-level array of API IDs",
            "missing" if "requirement_apis" not in payload else "null",
            'Add requirement_apis next to apis, not inside it, for example "requirement_apis": ["API-list-notes"].'),
            label, {"apis": sorted(apis, key=str)}, [f"apis.{name}" for name in sorted(apis, key=str)])
    if not isinstance(listed, list):
        raise unusable(shape_problem("requirement_apis", "an array of API IDs", type(listed).__name__,
            'Return {"requirement_apis": ["API-ID"]}; use [] only when this requirement needs no API.'),
            label, {"apis": sorted(apis, key=str)}, [f"apis.{name}" for name in sorted(apis, key=str)])
    wrong = [(index, item) for index, item in enumerate(listed)
        if not isinstance(item, str) or not item.strip()]
    accepted = set(apis) | {str(item) for item in known}
    unknown = [(index, item) for index, item in enumerate(listed)
        if isinstance(item, str) and item.strip() and item not in accepted]
    for problems, expected, fix in ((wrong, "an API ID string",
            "List API IDs as strings; a definition belongs under apis."),
            (unknown, "an API ID that this response's apis or an earlier requirement defines",
            "Define the API under apis in this answer, or list an API that already exists.")):
        if problems:
            index, item = problems[0]
            raise unusable(shape_problem(f"requirement_apis[{index}]", expected, repr(item), fix),
                label, {"apis": sorted(accepted, key=str)}, sorted(accepted, key=str))
    unique = list(dict.fromkeys(listed))
    if len(unique) != len(listed):
        notes.append("repeated API IDs in requirement_apis have been merged")
    return {"apis": deepcopy(apis), "requirement_apis": unique}, notes


def validate_design(design, obligations, complete=True, tree=None):
    """Compare read and write sets independently; no interface or schema policy."""
    expected = obligations["requirements"] if complete else design["requirements"]
    for rid in expected:
        if tree is not None and tree.children.get(rid):
            continue
        apis = [design["apis"][aid] for aid in design["requirements"].get(rid, [])]
        required = obligations["requirements"][rid]
        gaps = coverage(required, apis)
        if any(fields for gap in gaps.values() for fields in gap.values()):
            raise ObligationCoverageError(rid, required, union(apis), gaps)


def apply_decomposition(design, response, rid, obligations, tree=None):
    """Merge one proposal into a copy of the accepted design, then run both gates."""
    proposal, _ = read_api_proposal(response, rid, known=design.get("apis", {}))
    result = deepcopy(design)
    # One API ID merged twice is the same API updated, never a second route.
    result["apis"].update(deepcopy(proposal["apis"]))
    # The association is replaced for this requirement only: an earlier requirement keeps
    # the records it already passed the gates with, even when this proposal is rejected.
    result["requirements"][rid] = list(proposal["requirement_apis"])
    # Coverage first: it reports the missing read/write record the design model must fix.
    validate_design(result, obligations, complete=False, tree=tree)
    # Then the interface contract, so an unusable method, path or schema is rejected too.
    validate_contracts(result, tree=tree)
    return result

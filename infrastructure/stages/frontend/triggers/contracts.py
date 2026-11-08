"""Deterministic frontend-to-API contract checks for generated trigger code.

Step 11 records which canonical API each ordered trigger action calls; step 12 writes
the code that performs those calls. This module compares the written code with the
accepted API IR instead of trusting the model to respect it. Every
``context.api(API_ID, request)`` call must name an API that the trigger's own designed
actions declare, must fill exactly the route parameters of the API path, and may use
only the query, body and header fields the contract declares. A hidden query flag such
as ``detailTitle``, deleting through ``POST /api/books?deleteTitle=`` or creating a
child record through its parent collection therefore fail before any behavioural test
is allowed to accept the code.

Three rules keep the check honest:

* The IR is authoritative, not the handler. A field the contract does not declare is
  rejected even when the current handler would read it, because accepting it redefines
  the operation the backend satisfies.
* Legitimate reuse stays legal. A trigger may call any API its designed actions name,
  including one shared with other requirements; only substituting an unrelated
  operation for the designed one is rejected.
* Requests built dynamically (``context.api("API-x", request)``) cannot be verified
  statically. They pass here and are checked at request time by the compiler-owned
  client (``frontend/src/ui-api.ts``), which renders the same contract and records
  ``E2E_API_CONTRACT_ERROR``/``E2E_API_ROUTE_ERROR`` violations for the E2E fixture, so
  an undeclared field still cannot reach the backend and a swallowed error cannot make
  the violation invisible.
"""

import re

from urllib.parse import unquote

from infrastructure.shared.parameters import PARAMETER

# A request object accepts exactly these keys; anything else is an undeclared parameter.
REQUEST_PARTS = ("params", "query", "body", "headers")
# Fields that describe transport, not contract.
REQUEST_META = ("credentials",)
# Headers the host always owns; they never need a contract declaration.
SAFE_HEADERS = ("content-type", "accept", "accept-language", "x-requested-with")
# Requests that leave the canonical client bypass its method, path and contract checks.
BYPASS = re.compile(r"\b(?:fetch|XMLHttpRequest|sendBeacon)\s*\(|\baxios\b")
# The compiler-owned client reports the exact request it sent, so a 404 is diagnosable.
ROUTE_FAILURE = re.compile(r'E2E_API_ROUTE_ERROR: context\.api\("([^"]+)"\) sent ([A-Z]+) (\S+)')
CALL_START = re.compile(r"\.\s*api\s*(?:<[^<>()]*>)?\s*\(")
IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
OPENING = {"{": "}", "[": "]", "(": ")"}


class TriggerContractError(ValueError):
    """Report every request that contradicts the accepted API contract at once."""

    def __init__(self, problems: list[dict]) -> None:
        """Keep the structured problems available as repair feedback."""
        self.problems = problems
        self.feedback = {"problem_count": len(problems), "problems": problems}
        lines = [f"{len(problems)} frontend API contract problem(s):"]
        lines.extend(f"{index}. [{item['file']} -> {item['api'] or 'request'}] {item['field']}: "
            f"{item['reason']} Fix: {item['fix']}" for index, item in enumerate(problems, 1))
        super().__init__("\n".join(lines))


def path_parameters(path: str) -> list[str]:
    """Return the route parameter names of an API path in their documented order."""
    if not isinstance(path, str):
        return []
    return [match.group(1) or match.group(2) for match in PARAMETER.finditer(path)]


def api_contract(api: dict) -> dict | None:
    """Normalize one API IR member into the request fields its contract declares.

    ``None`` means the artifact carries no ``request`` object at all. Such an API keeps
    the transport-only client behaviour: there is nothing to compare against, and
    rejecting every field would fail a project whose step 06 artifact is incomplete
    rather than a trigger that contradicts a declared contract.
    """
    request = api.get("request") if isinstance(api, dict) else None
    if not isinstance(request, dict):
        return None
    signature = api.get("signature") or {}

    def fields(part):
        schema = request.get(part) if isinstance(request.get(part), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        return set(properties), {name for name in required if isinstance(name, str)}

    params, required_params = fields("params")
    query, required_query = fields("query")
    body, required_body = fields("body")
    headers, _ = fields("headers")
    route = path_parameters(signature.get("path"))
    return {"method": signature.get("method") or "GET", "path": signature.get("path") or "",
        "params": set(route), "required_params": set(route) | required_params,
        "query": query, "required_query": required_query,
        "body": body, "required_body": required_body, "headers": headers}


def contract_digest(apis: dict) -> dict:
    """Render the accepted request fields so the implementation model sees exact names."""
    digest = {}
    for aid, api in sorted(apis.items()):
        signature = (api or {}).get("signature") or {}
        entry = {"method": signature.get("method"), "path": signature.get("path")}
        contract = api_contract(api)
        if contract is None:
            entry["request"] = "This API model declares no request fields."
        else:
            entry.update({"params": sorted(contract["params"]), "query": sorted(contract["query"]),
                "required_query": sorted(contract["required_query"]), "body": sorted(contract["body"]),
                "required_body": sorted(contract["required_body"]), "headers": sorted(contract["headers"]),
                "note": "Only these request fields exist. There are no hidden parameters."})
        digest[aid] = entry
    return digest


def _trim(source: str, start: int, end: int) -> tuple[int, int]:
    """Narrow a range to its first and last significant characters."""
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    return start, end


def _skip(source: str, index: int) -> int:
    """Skip whitespace and comments so punctuation comparisons see real tokens."""
    while index < len(source):
        if source[index].isspace():
            index += 1
        elif source.startswith("//", index):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end + 1
        elif source.startswith("/*", index):
            end = source.find("*/", index)
            index = len(source) if end < 0 else end + 2
        else:
            break
    return index


def _string_end(source: str, index: int) -> int:
    """Return the index after the string or template literal that starts at index."""
    quote = source[index]
    index += 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if quote == "`" and source.startswith("${", index):
            index = _matching(source, index + 1)
            continue
        if char == quote:
            return index + 1
        if quote != "`" and char == "\n":
            return index
        index += 1
    return len(source)


def _matching(source: str, index: int) -> int:
    """Return the index just past the bracket that closes ``source[index]``."""
    opening = source[index]
    closing = OPENING[opening]
    depth = 0
    while index < len(source):
        char = source[index]
        if char in "\"'`":
            index = _string_end(source, index)
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            index = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index)
            index = len(source) if end < 0 else end + 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(source)


def _split(source: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split a range on top-level commas, ignoring strings, comments and nesting."""
    parts, index, current = [], start, start
    while index < end:
        char = source[index]
        if char in "\"'`":
            index = _string_end(source, index)
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = end if newline < 0 or newline > end else newline + 1
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index)
            index = end if close < 0 or close > end else close + 2
            continue
        if char in OPENING:
            index = min(_matching(source, index), end)
            continue
        if char == ",":
            parts.append((current, index))
            current = index + 1
        index += 1
    if _trim(source, current, end)[0] < _trim(source, current, end)[1]:
        parts.append((current, end))
    return parts


def _colon(source: str, start: int, end: int) -> int | None:
    """Return the index of the top-level ``:`` separating a key from its value."""
    index = start
    while index < end:
        char = source[index]
        if char in "\"'`":
            index = _string_end(source, index)
            continue
        if char in OPENING:
            index = min(_matching(source, index), end)
            continue
        if char == ":":
            return index
        index += 1
    return None


def _key(source: str, start: int, end: int) -> str | None:
    """Read an object key literally; a computed key cannot be checked and returns None."""
    start, end = _trim(source, start, end)
    if start >= end:
        return None
    if source[start] in "\"'`":
        finish = _string_end(source, start)
        if finish != end:
            return None
        text = source[start + 1:finish - 1]
        return None if "${" in text else text
    text = source[start:end]
    return text if IDENTIFIER.match(text) else None


def _object(source: str, index: int) -> dict:
    """Read the top-level entries of the object literal that starts at index."""
    end = _matching(source, index)
    entries, dynamic = [], False
    for start, stop in _split(source, index + 1, end - 1):
        start, stop = _trim(source, start, stop)
        if start >= stop:
            continue
        if source.startswith("...", start):
            dynamic = True
            continue
        colon = _colon(source, start, stop)
        name = _key(source, start, stop if colon is None else colon)
        if colon is None:
            entries.append({"name": name, "value": None})
            continue
        value_start = _skip(source, colon + 1)
        nested = None
        if value_start < stop and source[value_start] == "{" and _matching(source, value_start) <= stop:
            nested = _object(source, value_start)
        entries.append({"name": name, "value": nested})
    return {"entries": entries, "dynamic": dynamic}


def _literal(source: str, start: int, end: int) -> str | None:
    """Return the text of a range that is exactly one string literal, else None."""
    if start >= end or source[start] not in "\"'`":
        return None
    finish = _string_end(source, start)
    if finish != end:
        return None
    text = source[start + 1:finish - 1]
    return None if "${" in text else text


def _part(entry: dict) -> dict:
    """Describe one request part: its literal keys and whether it was built dynamically."""
    value = entry["value"]
    if value is None:
        return {"keys": set(), "dynamic": True}
    names = {item["name"] for item in value["entries"] if item["name"]}
    return {"keys": names, "dynamic": value["dynamic"] or any(item["name"] is None for item in value["entries"])}


def api_calls(source: str) -> list[dict]:
    """Collect every ``.api(API_ID, request)`` call with its literal request fields.

    The receiver is not inspected: the only ``api`` call a trigger source makes is the
    one the host supplies, and comparing the API ID against the accepted contract is
    what matters. The context parameter may be called ``context``, ``ctx`` or anything
    else.
    """
    calls = []
    for match in CALL_START.finditer(source or ""):
        opening = match.end() - 1
        end = _matching(source, opening)
        arguments = _split(source, opening + 1, end - 1)
        call = {"index": match.start(), "api": None, "dynamic_api": False,
            "parts": {}, "extra": set(), "arguments": len(arguments), "dynamic_request": False}
        if arguments:
            start, stop = _trim(source, *arguments[0])
            call["api"] = _literal(source, start, stop)
            call["dynamic_api"] = call["api"] is None
        if len(arguments) > 1:
            start, stop = _trim(source, *arguments[1])
            if start < stop and source[start] == "{" and _matching(source, start) <= stop:
                for item in _object(source, start)["entries"]:
                    name = item["name"]
                    if name in REQUEST_PARTS:
                        call["parts"][name] = _part(item)
                    elif name in REQUEST_META:
                        continue
                    elif name is None:
                        call["dynamic_request"] = True
                    else:
                        call["extra"].add(name)
            elif start < stop:
                call["dynamic_request"] = True
        calls.append(call)
    return calls


def _problem(label: str, api, field: str, reason: str, fix: str) -> dict:
    """Keep one record shape so the repair feedback reads the same for every rule."""
    return {"file": label or "trigger", "api": api, "field": field, "reason": reason, "fix": fix}


def _declared(aid, contract, kind: str, values: dict, label: str) -> list[dict]:
    """Report request fields the contract does not declare for one part."""
    allowed = contract[kind]
    if kind == "headers":
        allowed = allowed | set(SAFE_HEADERS)
    problems = []
    for name in sorted(values["keys"] - allowed):
        problems.append(_problem(label, aid, f"{kind}.{name}",
            f"{contract['method']} {contract['path']} declares no {kind} field {name!r} "
            f"(declared: {sorted(allowed) or 'none'})",
            f"Send only the {kind} fields the API model declares. A {kind} name the contract does "
            f"not declare cannot change the operation; if the workflow needs another operation, "
            f"call the canonical API that declares it."))
    return problems


def _fields_contract(aid, contract, call, label: str) -> list[dict]:
    """Reject undeclared or missing query/body/header fields for one request."""
    problems, parts = [], call["parts"]
    method, path = contract["method"], contract["path"]

    def part(name):
        # A request object built elsewhere cannot be inspected; the runtime client
        # still checks it before the request leaves the browser.
        if call.get("dynamic_request"):
            return {"keys": set(), "dynamic": True}
        return parts.get(name) or {"keys": set(), "dynamic": False}

    params, query, body = part("params"), part("query"), part("body")
    route = sorted(contract["params"])
    for name in sorted(params["keys"] - contract["params"]):
        problems.append(_problem(label, aid, f"params.{name}",
            f"{method} {path} declares no route parameter {name!r} (declared: {route or 'none'})",
            f"Send params only for {route or 'the declared route parameters'}; a value the path "
            f"does not name is never used by the request."))
    if not params["dynamic"]:
        for name in sorted(contract["required_params"] - params["keys"]):
            problems.append(_problem(label, aid, f"params.{name}",
                f"{method} {path} requires the route parameter {name!r}, which this call does not "
                f"supply",
                f"Send params {{ {name}: <value> }} read from the page data or the current route; "
                f"the compiler-owned client refuses to request a literal placeholder."))
    if contract["method"] in ("GET", "HEAD") and body["keys"]:
        problems.append(_problem(label, aid, "body",
            f"{method} {path} cannot carry request.body; the client sends no body for a read",
            "Send the declared query or route parameters instead, or call the canonical write API "
            "that performs this operation."))
    else:
        problems.extend(_declared(aid, contract, "body", body, label))
        for name in sorted(contract["required_body"] - body["keys"]) if not body["dynamic"] else []:
            problems.append(_problem(label, aid, f"body.{name}",
                f"{method} {path} requires the body field {name!r} (declared: "
                f"{sorted(contract['body'])})",
                f"Send the documented body including {name}; an API rejects a request that omits a "
                f"required field."))
    problems.extend(_declared(aid, contract, "query", query, label))
    for name in sorted(contract["required_query"] - query["keys"]) if not query["dynamic"] else []:
        problems.append(_problem(label, aid, f"query.{name}",
            f"{method} {path} requires the query parameter {name!r} (declared: "
            f"{sorted(contract['query'])})",
            f"Send query {{ {name}: <value> }} using the documented parameter name."))
    problems.extend(_declared(aid, contract, "headers", part("headers"), label))
    return problems


def call_problems(backend, source, declared=None, label: str = "") -> list[dict]:
    """Check every request in one source file against the accepted API contract."""
    apis = (backend or {}).get("apis") or {}
    problems = []
    for match in BYPASS.finditer(source or ""):
        token = match.group(0).split("(")[0].strip()
        problems.append(_problem(label, None, "network",
            f"`{token}` bypasses context.api, so the canonical HTTP method, path encoding and "
            f"request contract are not used",
            "Send business requests through context.api(API_ID, {params, query, body}); the "
            "compiler-owned client owns the method, the path and the credentials."))
    for call in api_calls(source or ""):
        problems.extend(_call_problems(apis, call, declared, label))
    return problems


def _call_problems(apis, call, declared, label) -> list[dict]:
    """Reject an invented, undeclared or mis-shaped request for one parsed call."""
    aid = call["api"]
    if call["dynamic_api"]:
        return [_problem(label, None, "api_id",
            "context.api received a computed API ID, so the request cannot be checked against the "
            "accepted contract",
            "Pass the canonical API ID as a string literal, for example "
            'context.api("API-get-book-details", {params: {title}}).')]
    api = apis.get(aid)
    if api is None:
        return [_problem(label, aid, "api_id",
            f"{aid!r} is not part of the accepted backend contract",
            "Call one of the API IDs supplied in backend_apis; do not invent endpoints or verbs.")]
    contract = api_contract(api)
    route = f"{contract['method']} {contract['path']}" if contract else "the API"
    problems = []
    for name in sorted(call["extra"]):
        problems.append(_problem(label, aid, name,
            f"request key {name!r} is not part of a context.api request; only params, query, body "
            f"and headers carry values",
            f"Move the value into a field {route} declares, or drop it. The client ignores an "
            f"undeclared request key, so it silently changes nothing."))
    if declared is not None and aid not in declared:
        designed = sorted(declared)
        problems.append(_problem(label, aid, "api",
            f"this trigger's designed actions call {designed or 'no API'}, so {aid} is not the "
            f"operation it was designed to perform",
            f"Call the designed API ({designed}) with its documented request. Reusing an API shared "
            f"with another requirement is fine when the operation is the same; replacing the "
            f"operation is not."))
    if contract is not None:
        problems.extend(_fields_contract(aid, contract, call, label))
    return problems


def contract_problems(backend, files) -> list[dict]:
    """Audit ``(path, source[, declared API IDs])`` entries.

    A missing ``declared`` entry means the file is a shared helper rather than a trigger:
    the operation-substitution rule needs a designed action to compare with, so only the
    request shape is checked there.
    """
    problems = []
    for entry in files or ():
        label, source = entry[0], entry[1]
        declared = entry[2] if len(entry) > 2 else None
        problems.extend(call_problems(backend, source, declared, label))
    return problems


def contract_report(backend, files) -> dict:
    """Return a check-shaped result so contract violations gate acceptance like tests do."""
    files = list(files or ())
    problems = contract_problems(backend, files)
    calls = sum(len(api_calls(entry[1] or "")) for entry in files)
    apis = len((backend or {}).get("apis") or {})
    if problems:
        return {"returncode": 1, "problems": problems, "calls": calls, "apis": apis,
            "output": str(TriggerContractError(problems))}
    return {"returncode": 0, "problems": [], "calls": calls, "apis": apis,
        "output": f"{calls} context.api call(s) match the accepted contract of {apis} API(s)"}


def require_contract(result) -> dict:
    """Accept trigger code only when every request matches the accepted contract."""
    if result.get("problems"):
        raise ValueError(result.get("output") or "Frontend API contract violated")
    return result


def _segments(path) -> list[str]:
    """Split a request path into comparable segments."""
    return [segment for segment in str(path or "").split("?")[0].split("/") if segment]


def _route_matches(signature: dict, method: str, path: str) -> bool:
    """Report whether a declared signature is the route this failing request addressed."""
    if str(signature.get("method") or "").upper() != method:
        return False
    declared, requested = _segments(signature.get("path")), _segments(path)
    return len(declared) == len(requested) and all(
        bool(PARAMETER.fullmatch(segment)) or segment == other
        for segment, other in zip(declared, requested))


def route_problems(backend, failures=()) -> list[dict]:
    """Locate the route behind an ``Unknown API route`` failure instead of switching APIs.

    The compiler-owned client reports the exact request it sent before the backend
    answered 404, so the repair goes to that request or to the generated route
    registration. Substituting another business API is explicitly excluded: it would
    hide a routing defect and change the operation the requirement asks for.
    """
    apis = (backend or {}).get("apis") or {}
    text = "\n".join(failure if isinstance(failure, str) else str(failure) for failure in failures or ())
    problems, seen = [], set()
    for match in ROUTE_FAILURE.finditer(text):
        aid, method, path = match.group(1), match.group(2), unquote(match.group(3))
        if (aid, method, path) in seen:
            continue
        seen.add((aid, method, path))
        signature = (apis.get(aid) or {}).get("signature") or {}
        route = (f"{signature.get('method')} {signature.get('path')}" if signature
            else f"an accepted API named {aid}")
        candidates = [key for key, api in sorted(apis.items())
            if _route_matches((api or {}).get("signature") or {}, method, path)]
        reason = (f"context.api({aid!r}) sent {method} {path} and the backend answered 404 Unknown "
            f"API route; the accepted contract of {aid} is {route}")
        if candidates:
            reason += f", and that path shape is registered for {', '.join(candidates)}"
        problems.append(_problem(aid, aid, "route", reason,
            f"Rebuild the request from {route}: only declared route parameters fill the path, so a "
            f"wrong or empty value produces an unroutable URL. Fix the request or the generated "
            f"route registration (backend/src/api/register.ts) instead of calling a different "
            f"business API to hide the missing route."))
    if not seen and "Unknown API route" in text:
        problems.append(_problem("", None, "route",
            "the backend answered 404 Unknown API route, so a business request reached no "
            "registered handler",
            "Keep the designed API and fix the request path or the generated route registration "
            "(backend/src/api/register.ts) so the documented route reaches its handler; do not "
            "replace the call with a different API."))
    return problems

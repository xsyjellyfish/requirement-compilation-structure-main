"""Stage 08 HTTP integration tests: replay the API IR over the generated Express app.

The handler-level suites that step 08 generates prove business behaviour, but they call
each implementation directly. They therefore cannot observe whether a route is
registered, whether Express dispatches the declared method and path parameter, whether
the JSON body parser ran, or whether the response actually reaches a client. A backend
can be fully GREEN at handler level while its HTTP surface is unusable.

This module renders a second, compiler-owned suite. It boots ``createApp(connection)``
on an ephemeral loopback port against an isolated in-memory SQLite database, issues real
HTTP requests, and asserts status codes, response bodies and database side effects.

Rendering is deterministic: the API IR alone decides which probes and which CRUD flows
exist, so one design always produces the same suite and no model call is required.
"""

import json
import re
from hashlib import sha256
from pathlib import Path

from infrastructure.shared.api.paths import express_path
from infrastructure.shared.parameters import PARAMETER, WHOLE_PARAMETER
from infrastructure.shared.project.entrypoints import import_path
from infrastructure.stages.backend.http_runtime import http_support_source
from infrastructure.stages.backend.testing import failure_blocks
from infrastructure.utils.files import save_text

SUPPORT_PATH = "backend/tests/support/http.ts"
FLOW_PATH = "backend/tests/http/flows/crud.test.ts"
SUITE_ROOT = "backend/tests/http"
BODY_METHODS = ("POST", "PUT", "PATCH")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ALTERNATE_METHODS = ("POST", "GET", "PUT", "PATCH", "DELETE")
PROBE_VALUE = "arc-http-probe"


def normalize_name(value: str) -> str:
    """Normalize a property, column or parameter name the way the emitted harness does."""
    return "".join(character for character in str(value).lower() if character.isalnum())


def singular(word: str) -> str:
    """Reduce a path segment to the entity name it collects: books -> book, pages -> page."""
    lowered = str(word).lower()
    if lowered.endswith("ies") and len(lowered) > 3:
        return lowered[:-3] + "y"
    if lowered.endswith(("ches", "shes", "sses", "xes", "zes")):
        return lowered[:-2]
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def slug(value: str) -> str:
    """Build one filesystem-safe path segment, hashing names unsuitable for paths."""
    text = str(value)
    return text if text and all(character.isalnum() or character in "_-" for character in text) else "id-" + sha256(text.encode("utf-8")).hexdigest()[:12]


def owner_requirement(design: dict, aid: str) -> str:
    """Return the first requirement that lists the API; the IR is acyclic and complete."""
    return next((rid for rid, ids in (design.get("requirements") or {}).items() if aid in ids), aid)


def suite_path(design: dict, aid: str) -> str:
    """One probe suite per API so a single implementation can be gated on its own."""
    return f"{SUITE_ROOT}/{slug(owner_requirement(design, aid))}/{slug(aid)}.http.test.ts"


def parameters(route: str) -> list[str]:
    """List the path-parameter names of an Express route template, in path order."""
    return [match.group(1) or match.group(2) for match in PARAMETER.finditer(route)]


def concrete_path(route: str) -> str:
    """Sample a route with sentinel values so every parameter is requested for real."""
    count = 0

    def replace(match):
        """Substitute one parameter with a value that cannot exist in the database."""
        nonlocal count
        count += 1
        return f"{PROBE_VALUE}-{count}"

    return PARAMETER.sub(replace, route)


def success_status(api: dict) -> int:
    """Read the documented success status of one API IR member."""
    return api["response"]["success"]["status"]


def error_statuses(api: dict) -> list[int]:
    """Read the documented rejection statuses of one API IR member."""
    return [error["status"] for error in api["response"]["errors"]]


def required_body(api: dict) -> list[str]:
    """Return the request body members the contract marks as required."""
    body = (api.get("request") or {}).get("body") or {}
    properties = body.get("properties") or {}
    return [name for name in (body.get("required") or []) if name in properties]


def probe_body(api: dict) -> dict | None:
    """Send an empty JSON body where the contract declares a body; nothing otherwise."""
    return {} if api["signature"]["method"].upper() in BODY_METHODS else None


def alternative_method(design: dict, aid: str, route: str) -> str | None:
    """Pick a method that is deliberately not declared on this route template."""
    declared = {(api["signature"]["method"].upper(), express_path(api["signature"]["path"]))
        for api in design["apis"].values()}
    method = design["apis"][aid]["signature"]["method"].upper()
    return next((candidate for candidate in ALTERNATE_METHODS
        if candidate != method and (candidate, route) not in declared), None)



def probe_cases(design: dict, aid: str) -> list[dict]:
    """Describe the HTTP probes one API must survive, in a form the harness consumes."""
    api = design["apis"][aid]
    method = api["signature"]["method"].upper()
    route = express_path(api["signature"]["path"])
    concrete = concrete_path(route)
    statuses, success = error_statuses(api), success_status(api)
    cases = [{"api": aid, "kind": "route", "expect": "registered",
        "title": f"{method} {route} is registered on the Express app",
        "method": method, "path": concrete, "body": probe_body(api), "statuses": statuses, "success": success}]
    other = alternative_method(design, aid, route)
    if other:
        cases.append({"api": aid, "kind": "method", "expect": "unknownRoute",
            "title": f"{other} {route} is not routed instead of {method}",
            "method": other, "path": concrete, "body": {} if other in BODY_METHODS else None,
            "statuses": statuses, "success": success})
    if parameters(route) and method == "GET" and statuses:
        cases.append({"api": aid, "kind": "unknown-param", "expect": "documentedError",
            "title": f"GET {route} rejects an identifier that cannot exist",
            "method": "GET", "path": concrete, "body": None, "statuses": statuses, "success": success})
    if method in WRITE_METHODS and api.get("write") and statuses and required_body(api):
        cases.append({"api": aid, "kind": "rejection", "expect": "rejectionWithoutWrite",
            "title": f"{method} {route} rejects a request with missing required fields and writes nothing",
            "method": method, "path": concrete, "body": {}, "statuses": statuses, "success": success})
    return cases


def scalar_type(schema) -> str | None:
    """Return the first JSON scalar type a schema admits, ignoring nullable wrappers."""
    declared = schema.get("type") if isinstance(schema, dict) else None
    for candidate in (declared if isinstance(declared, list) else [declared]):
        if candidate in {"string", "number", "integer", "boolean"}:
            return candidate
    return None


#: Values that honour a declared ``format``; the contract itself asks for that shape, so
#: sending one invents nothing the design did not already require.
FORMAT_VALUES = {"date": "2030-01-01", "date-time": "2030-01-01T00:00:00.000Z", "time": "12:00:00",
    "uuid": "00000000-0000-4000-8000-000000000000"}
FORMAT_SUFFIXES = {"email": "@example.test", "uri": "://example.test/probe", "url": "://example.test/probe",
    "hostname": ".example.test", "ipv4": ""}
#: A property that only repeats another one; a real client sends the same value twice.
CONFIRMATION = re.compile(r"(?:confirm|confirmation|repeat|validate)[-_ ]?(.+)|(.+)[-_]?(?:confirm(?:ation)?|repeat)\Z",
    re.IGNORECASE)


def synthesized_string(schema: dict, seed: str) -> str:
    """Build a string the contract admits, honouring a declared format when it names one."""
    marker = f"{PROBE_VALUE}-{seed}"
    declared = schema.get("format")
    if declared in FORMAT_VALUES:
        return FORMAT_VALUES[declared]
    if declared in FORMAT_SUFFIXES:
        return marker + FORMAT_SUFFIXES[declared]
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and not isinstance(minimum, bool) and len(marker) < minimum:
        return marker + "x" * (minimum - len(marker))
    return marker


def literal_value(schema: dict) -> tuple[object, bool]:
    """Return a value the schema itself declares, if it declares one."""
    if "const" in schema:
        return schema["const"], True
    allowed = schema.get("enum")
    if isinstance(allowed, list) and allowed:
        return allowed[0], True
    if schema.get("default") is not None:
        return schema["default"], True
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0], True
    return None, False


def synthesize(schema, seed: str, depth: int = 0) -> tuple[object, bool]:
    """Build one JSON value the contract admits, or report that the design gives none."""
    if not isinstance(schema, dict):
        return None, False
    for keyword in ("anyOf", "oneOf", "allOf"):
        options = [option for option in (schema.get(keyword) or []) if isinstance(option, dict)]
        if options:
            return synthesize(options[0], seed, depth)
    value, declared = literal_value(schema)
    if declared:
        return value, True
    kind = scalar_type(schema)
    if kind == "string":
        return synthesized_string(schema, seed), True
    if kind in {"number", "integer"}:
        return 4242, True
    if kind == "boolean":
        return True, True
    if schema.get("type") == "object" and depth < 2:
        return synthesize_object(schema, seed, depth)
    if schema.get("type") == "array" and depth < 2:
        item, usable = synthesize(schema.get("items") or {}, seed, depth + 1)
        return ([item], True) if usable else (None, False)
    return None, False


def synthesizable(schema, depth: int = 0) -> bool:
    """Report whether a required value can be built from the contract alone."""
    return synthesize(schema, "probe", depth)[1]


def underivable(api: dict) -> list[str]:
    """List the required body members whose value the contract does not define."""
    body = (api.get("request") or {}).get("body") or {}
    properties = body.get("properties") or {}
    return [name for name in required_body(api) if not synthesizable(properties[name])]


def confirmation_target(api: dict, name: str) -> str | None:
    """Name the property a confirmation member repeats: confirm_password -> password."""
    properties = (api.get("request") or {}).get("body", {}).get("properties") or {}
    normalized = {normalize_name(key): key for key in properties}
    match = CONFIRMATION.match(name)
    if match is None:
        return None
    for candidate in (match.group(1), match.group(2)):
        if candidate and normalize_name(candidate) in normalized:
            return normalized[normalize_name(candidate)]
    return None


def synthesize_object(schema, seed: str, depth: int = 0) -> tuple[dict | None, bool]:
    """Fill the required members of an object schema; optional members are best effort."""
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict) or not properties:
        return None, False
    required = [name for name in (schema.get("required") or []) if name in properties]
    example = {}
    for name in required or list(properties):
        value, usable = synthesize(properties[name], f"{seed}-{name}", depth + 1)
        if usable:
            example[name] = value
        elif name in required:
            return None, False
    return (example, True) if example else (None, False)


def body_example(api: dict, seed: str) -> dict | None:
    """Return a request body the contract admits, or None when none can be derived."""
    example, usable = synthesize_object((api.get("request") or {}).get("body") or {}, seed)
    if not usable:
        return None
    for name in list(example):
        target = confirmation_target(api, name)
        if target is not None and target in example:
            example[name] = example[target]
    return example


def sentinel(body) -> str | None:
    """Pick the submitted marker that a response or the database must contain."""
    if isinstance(body, str):
        return body if body.startswith(PROBE_VALUE) else None
    if isinstance(body, dict):
        return next((found for value in body.values() if (found := sentinel(value))), None)
    if isinstance(body, list):
        return next((found for value in body if (found := sentinel(value))), None)
    return None


def value_names(param: str, previous: str) -> list[str]:
    """Map a route parameter onto the created value it addresses: bookTitle -> title."""
    name = normalize_name(param)
    base = normalize_name(singular(previous)) if previous else ""
    names = []
    if base and len(name) > len(base) and name.startswith(base):
        names.append(name[len(base):])
    names.append(name)
    return names


def resolve(route: str, scopes: dict) -> tuple[str, bool]:
    """Replace each route parameter with the placeholder of a value created earlier."""
    segments = route.strip("/").split("/")
    rendered = []
    for index, segment in enumerate(segments):
        match = WHOLE_PARAMETER.fullmatch(segment)
        if match is None:
            rendered.append(segment)
            continue
        available = scopes.get("/" + "/".join(rendered)) or {}
        previous = segments[index - 1] if index else ""
        token = next((available[key] for key in value_names(match.group(1) or match.group(2), previous)
            if key in available), None)
        if token is None:
            return route, False
        rendered.append("{{" + token + "}}")
    return "/" + "/".join(rendered), True


def item_route(design: dict, methods: dict, routes: dict, owner: str, base: str, scopes: dict, wanted: tuple) -> tuple[str, str] | None:
    """Find the item route of one collection that addresses exactly one entity.

    A direct item route (its last segment is the parameter) is preferred over a deeper
    sub-resource route such as ``/api/books/:bookTitle/pages``, which also adds exactly
    one parameter but describes a collection rather than the created record.
    """
    candidates = []
    for aid in sorted(design["apis"]):
        if methods[aid] not in wanted or len(parameters(routes[aid])) != len(parameters(routes[owner])) + 1:
            continue
        resolved, usable = resolve(routes[aid], scopes)
        if not usable or not resolved.startswith(base + "/"):
            continue
        segments = routes[aid].strip("/").split("/")
        candidates.append((0 if WHOLE_PARAMETER.fullmatch(segments[-1]) else 1, len(segments), aid, resolved))
    if not candidates:
        return None
    _, _, aid, resolved = min(candidates)
    return aid, resolved


def returns_value(api: dict, param: str) -> bool:
    """Whether the success body documents the value the item route parameter addresses."""
    body = (api["response"]["success"] or {}).get("body") or {}
    properties = body.get("properties") or {}
    wanted = normalize_name(param)
    return isinstance(properties, dict) and any(normalize_name(key) == wanted for key in properties)


def echo_value(api: dict, body) -> str | None:
    """Pick the submitted marker the success body documents, so a create must echo it.

    A response that promises ``username`` has to carry the ``username`` that was submitted;
    a marker only counts when the contract names that response member.
    """
    properties = ((api["response"]["success"] or {}).get("body") or {}).get("properties") or {}
    documented = {normalize_name(name) for name in properties} if isinstance(properties, dict) else set()
    for name, value in (body or {}).items():
        if isinstance(value, str) and value.startswith(PROBE_VALUE) and normalize_name(name) in documented:
            return value
    return None


#: A precondition that needs a record the flow must create through HTTP first: a sign-in
#: checks an existing account, while "no existing username" only forbids a duplicate row.
EXISTING_RECORD = re.compile(
    r"\b(?:a|an|the)\s+(?:\w+\s+){0,3}(?:record|row|account|user|session|member|entity|resource|item|entry|profile)"
    r"\s+(?:already\s+)?exists?\b"
    r"|\b(?:already|previously)\s+(?:registered|created|signed[- ]?in|authenticated|stored)\b"
    r"|\b(?:signed[- ]?in|logged[- ]?in|authenticated)\b", re.IGNORECASE)


def needs_givens(api: dict) -> bool:
    """Whether a create reads a record that must already exist before the flow starts."""
    return any(EXISTING_RECORD.search(str(entry.get("description", "")))
        for entry in (api.get("precondition") or []) if isinstance(entry, dict))


def step(kind: str, aid: str, api: dict, path: str, *, body=None, contains=None, scope=None,
        check_body=False, echo=None) -> dict:
    """Describe one HTTP step of a CRUD flow, keeping the IR's documented statuses.

    ``required`` marks the steps whose documented success status is part of the business
    flow: a documented 4xx on one of them means the flow was not exercised, never that it
    passed. The final ``read-empty`` step is required to be rejected instead.
    """
    return {"kind": kind, "api": aid, "method": api["signature"]["method"].upper(), "path": path,
        "body": body, "success": success_status(api), "statuses": error_statuses(api),
        "contains": contains, "scope": scope, "checkBody": check_body, "echo": echo,
        "required": kind != "read-empty"}



def step_list(design: dict, methods: dict, routes: dict, path: str, scopes: dict, created: str | None) -> list[dict]:
    """Read back the collection that was just written, when the contract declares it."""
    if created is None:
        return []
    for aid in sorted(design["apis"]):
        if methods[aid] != "GET":
            continue
        resolved, usable = resolve(routes[aid], scopes)
        if usable and resolved == path:
            return [step("list", aid, design["apis"][aid], resolved, contains=created)]
    return []


def path_tokens(path: str) -> list[str]:
    """List the value placeholders one resolved path substitutes."""
    return re.findall(r"\{\{([^}]+)\}\}", path)


def creating_apis(design: dict) -> list[str]:
    """List the APIs that create a record from caller data, so each needs a success flow.

    A ``POST`` that persists nothing (sign-out) or accepts no payload (a session driven by
    a cookie) is covered by its probe suite alone; a ``POST`` that writes a record from body
    values is the one whose success journey a derived flow has to execute, because its 4xx
    rejections would otherwise be the only evidence that the API exists.
    """
    return [aid for aid, api in sorted(design["apis"].items())
        if api["signature"]["method"].upper() == "POST" and api.get("write")
        and ((api.get("request") or {}).get("body") or {}).get("properties")]


def plan_flows(design: dict) -> tuple[list[dict], list[dict]]:
    """Derive create/list/read/update/delete chains from the IR path structure.

    Every step is a real HTTP request. The columns of a created row become the path
    parameters of the later steps, so nested resources such as
    ``/api/books/{bookTitle}/pages/{pageTitle}`` are addressed with real identifiers.
    A nested flow repeats the creates it depends on, so each flow is self-contained.

    A create whose precondition signs in an existing record is planned after the flow that
    creates that record, and borrows its submitted values, so a sign-in flow proves the
    business journey instead of stopping at the ``401`` of an empty database. Every create
    no flow can exercise is returned as a note, so a missing success flow is reported
    instead of silently disappearing from the suite.
    """
    apis, scopes, flows, notes = design["apis"], {}, [], []
    setups: dict[str, list[dict]] = {}
    routes = {aid: express_path(api["signature"]["path"]) for aid, api in apis.items()}
    methods = {aid: api["signature"]["method"].upper() for aid, api in apis.items()}
    creates = sorted((aid for aid in apis if methods[aid] == "POST"),
        key=lambda aid: (len(parameters(routes[aid])), routes[aid].count("/"), aid))
    # Values other flows already submit, by normalized property name: the record a sign-in
    # authenticates is the account another create made moments earlier.
    provided: dict[str, tuple[str, str]] = {}
    ordered = [aid for aid in creates if not needs_givens(apis[aid])] \
        + [aid for aid in creates if needs_givens(apis[aid])]
    for aid in ordered:
        api = apis[aid]
        body = body_example(api, f"create-{slug(aid)}")
        if body is None:
            properties = ((api.get("request") or {}).get("body") or {}).get("properties") or {}
            if properties:
                notes.append({"api": aid, "kind": "payload",
                    "reason": "the contract declares request.body properties but no success payload can be "
                        "derived for " + (", ".join(underivable(api)) or ", ".join(properties))})
            continue
        path, usable = resolve(routes[aid], scopes)
        if not usable:
            notes.append({"api": aid, "kind": "route",
                "reason": f"{routes[aid]} needs a route value that no earlier create in the flow provides"})
            continue
        setup, seen = [], set()
        for token in path_tokens(path):
            for required in setups.get(token.rsplit("::", 1)[0], []):
                key = (required["api"], required["path"])
                if key not in seen:
                    seen.add(key)
                    setup.append(required)
        if needs_givens(api):
            borrowed = {name: provided[normalize_name(name)] for name in body if normalize_name(name) in provided}
            if not borrowed:
                notes.append({"api": aid, "kind": "given",
                    "reason": "the success flow authenticates an existing record and no other API's flow creates "
                        "one: " + "; ".join(str(entry.get("description", ""))
                            for entry in api.get("precondition") or [] if isinstance(entry, dict))})
                continue
            for provider in {scope for scope, _ in borrowed.values()}:
                for required in setups.get(provider, []):
                    key = (required["api"], required["path"])
                    if key not in seen:
                        seen.add(key)
                        setup.append(required)
            body = {name: "{{" + token + "}}" for name, (_, token) in borrowed.items()}
        for name, value in body.items():
            if isinstance(value, str) and value.startswith(PROBE_VALUE):
                provided[normalize_name(name)] = (path, f"{path}::{normalize_name(name)}")
        scopes.setdefault(path, {}).update({normalize_name(name): f"{path}::{normalize_name(name)}"
            for name in body})
        created = sentinel(body)
        create = step("create", aid, api, path, body=body, contains=created, scope=path,
            echo=echo_value(api, body))
        steps = [create]
        steps.extend(step_list(design, methods, routes, path, scopes, created))
        read = item_route(design, methods, routes, aid, path, scopes, ("GET",))
        update = item_route(design, methods, routes, aid, path, scopes, ("PATCH", "PUT"))
        delete = item_route(design, methods, routes, aid, path, scopes, ("DELETE",))
        if read:
            param = parameters(routes[read[0]])[-1]
            steps.append(step("read", read[0], apis[read[0]], read[1],
                contains=created, check_body=returns_value(apis[read[0]], param)))
        if update:
            update_body = body_example(apis[update[0]], f"update-{slug(update[0])}")
            if update_body is not None:
                steps.append(step("update", update[0], apis[update[0]], update[1],
                    body=update_body, contains=sentinel(update_body)))
        if delete:
            steps.append(step("delete", delete[0], apis[delete[0]], delete[1], contains=created))
        if read and delete:
            steps.append(step("read-empty", read[0], apis[read[0]], read[1], contains=created))
        setups[path] = [*setup, create]
        flows.append({"resource": routes[aid], "steps": [*setup, *steps]})
    # Every create a flow exercises is proven; every record-creating API no flow reached is
    # reported, so a success journey cannot disappear from the suite without a reason.
    exercised = {step["api"] for flow in flows for step in flow["steps"] if step["kind"] == "create"}
    for aid in creating_apis(design):
        if aid in exercised or any(note["api"] == aid for note in notes):
            continue
        notes.append({"api": aid, "kind": "flow",
            "reason": f"{routes[aid]} writes a record but no derived flow creates one and reads it back, "
                "so its success journey would stay unverified"})
    return flows, notes


def flow_plan(design: dict) -> list[dict]:
    """Return only the derived flows; :func:`plan_flows` also reports the ones it cannot build."""
    return plan_flows(design)[0]


def flow_verdict(result: dict) -> str:
    """Classify a flow-suite run as ``PASS``, ``FAIL`` or ``UNVERIFIED``.

    A flow passes only when every documented success status was observed together with the
    response fields and the database side effects the contract promises. A documented
    rejection (400/401) proves nothing about the journey, so a run that only ever reached
    one is ``UNVERIFIED`` rather than a pass: reporting it as a pass is the green-wash this
    gate exists to prevent, and reporting it as a failure would overstate the evidence.
    """
    state = flow_result(result)
    blocks = failure_blocks(state.get("output") or "")
    if not state.get("tests"):
        # Nothing ran, so nothing was observed: an empty suite is never a pass.
        return "UNVERIFIED"
    if not state.get("returncode") and not state.get("fail"):
        return "PASS"
    return "UNVERIFIED" if blocks and all("was not verified" in block["text"].lower()
        for block in blocks) else "FAIL"


def unverified_flows(result: dict) -> list[str]:
    """Name the flows a run could only observe through documented rejections."""
    return [block["name"] for block in failure_blocks(flow_result(result).get("output") or "")
        if "was not verified" in block["text"].lower()]


def flow_result(result) -> dict:
    """Read the command result out of a final-check entry, or the result itself."""
    state = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
    return state if isinstance(state, dict) else {}


def probe_suite_source(design: dict, aid: str) -> str:
    """Render the compiler-owned probe suite for one API."""
    imported = import_path(suite_path(design, aid), SUPPORT_PATH)
    cases = json.dumps(probe_cases(design, aid), ensure_ascii=False, indent=2)
    return "\n".join([
        "// Compiler-owned HTTP contract tests for one API; step 08 regenerates this file.",
        f"import {{ contractTests, type ProbeCase }} from {json.dumps(imported)};",
        "",
        f"const cases: ProbeCase[] = {cases};",
        "",
        "contractTests(cases);",
        "",
    ])


def flow_suite_source(flows: list[dict]) -> str:
    """Render the compiler-owned cross-API CRUD suite."""
    imported = import_path(FLOW_PATH, SUPPORT_PATH)
    return "\n".join([
        "// Compiler-owned HTTP CRUD flows across APIs; step 08 regenerates this file.",
        f"import {{ crudFlowTests, type CrudFlow }} from {json.dumps(imported)};",
        "",
        f"const flows: CrudFlow[] = {json.dumps(flows, ensure_ascii=False, indent=2)};",
        "",
        "crudFlowTests(flows);",
        "",
    ])


def prepare_http_tests(output: Path, design: dict) -> dict:
    """Publish the HTTP harness, one probe suite per API, and the cross-API CRUD suite."""
    save_text(output / SUPPORT_PATH, http_support_source())
    files, probes = {}, 0
    for aid in sorted(design["apis"]):
        relative = suite_path(design, aid)
        save_text(output / relative, probe_suite_source(design, aid))
        files[aid] = relative
        probes += len(probe_cases(design, aid))
    flows, notes = plan_flows(design)
    save_text(output / FLOW_PATH, flow_suite_source(flows))
    return {"support": SUPPORT_PATH, "files": files, "probes": probes,
        "flows": {"file": FLOW_PATH, "resources": [flow["resource"] for flow in flows],
            "steps": sum(len(flow["steps"]) for flow in flows),
            "apis": sorted({step["api"] for flow in flows for step in flow["steps"]}),
            "notExercised": notes}}

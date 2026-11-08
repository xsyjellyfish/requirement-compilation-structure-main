"""Deterministic static checks for a backend API interface contract.

Step 06 proposes one requirement's APIs at a time and step 07 reviews the accepted
artifact, so both passes call :func:`validate_contracts`. A method, path, parameter,
identity, reference or schema that the later code generators cannot use is therefore
reported before any backend source is written. Every problem names the API, the field,
the reason and the correction; the same records are the repair feedback the design
model reads, so an invalid design is never accepted silently.

The rules deliberately ignore optional JSON Schema keywords. A schema that adds
``description``, ``format``, ``enum``, ``default``, ``additionalProperties`` or any
other legal keyword is valid; only the parts this compiler emits are checked.

Semantic reuse is a contract rule too. Two create operations for one entity are two
business goals that should have been one API plus two Requirement -> API edges, so a
second ``POST`` for an entity another API already creates is rejected, as is a create
that drops a required body field, a persisted column or a precondition an accepted
create of the same entity established. The rejection never deletes business behaviour:
it tells the design model which existing API ID to reuse and where to record the
many-to-many requirement association.
"""

import re

from infrastructure.shared.parameters import BRACES, DANGLING, PARAMETER

# Express only routes these methods, and the IR spells them in upper case.
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
# Every API needs these properties so one handler and one contract stay in step.
API_PROPERTIES = {
    "description": "one sentence about the business operation",
    "read": "array of qualified DB fields the operation reads",
    "write": "array of qualified DB fields the operation writes",
    "signature": "{name, method, path} of the generated handler",
    "request": "object schemas for params, query, body and headers",
    "response": "{success: {status, body, headers}, errors: [{status, body, headers}]}",
    "precondition": "[{id, description, error_status}], [] when unconditional",
    "postcondition": "[{id, description, status}], [] when nothing is observable",
}
REQUEST_PARTS = ("params", "query", "body", "headers")
RESPONSE_PARTS = ("body", "headers")
# signature.name becomes a TypeScript export, and the API ID names a test file.
IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
API_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
QUALIFIED_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\Z")
# A route may not carry a query, fragment, backslash, quote or shell-like separator.
FORBIDDEN_PATH_CHARACTERS = '?#\\"<>|'
# The compiler-owned HTTP flow has to build a success payload from the contract alone, so
# a required request value must declare a concrete type instead of an opaque shape.
SYNTHESIZABLE_KINDS = ("string", "integer", "number", "boolean")
VALUE_KEYWORDS = ("const", "default", "examples", "enum")



class ApiContractError(ValueError):
    """Report every invalid interface field at once, with the fix the design model needs."""

    def __init__(self, problems: list[dict]) -> None:
        """Keep the structured problems available as proposal repair feedback."""
        self.problems = problems
        self.feedback = {"problem_count": len(problems), "problems": problems}
        lines = [f"{len(problems)} backend API contract problem(s):"]
        lines.extend(
            f"{index}. [{item['api_id'] or 'design'}] {item['field']}: {item['reason']} Fix: {item['fix']}"
            for index, item in enumerate(problems, 1)
        )
        super().__init__("\n".join(lines))


def known_requirements(tree) -> set | None:
    """Read the requirement IDs a design may reference, or None when they are unknown."""
    if tree is None:
        return None
    return set(getattr(tree, "requirements", tree))


def validate_contracts(design, tree=None) -> dict:
    """Reject an interface contract the backend generators cannot implement."""
    problems = contract_problems(design, tree=tree)
    if problems:
        raise ApiContractError(problems)
    return design


def contract_problems(design, tree=None) -> list[dict]:
    """List every interface problem in declaration order instead of stopping at the first."""
    return _Checker(known_requirements(tree)).design(design)


def format_contract_summary(design) -> str:
    """Render the verified interface inventory and the rules that produced it."""
    apis = design.get("apis", {}) if isinstance(design, dict) else {}
    lines = [f"VERIFIED {len(apis)} API CONTRACT(S)"]
    for api_id, api in apis.items():
        signature = api.get("signature", {}) if isinstance(api, dict) else {}
        path = str(signature.get("path", ""))
        names = [match.group(1) or match.group(2) for match in PARAMETER.finditer(path)]
        lines.append(f"  {api_id:<32} {str(signature.get('method', '')):<7} {path}"
            + (f"  params={','.join(names)}" if names else ""))
    lines.extend(["", "RULES APPLIED",
        "  signature.method is one of " + ", ".join(METHODS),
        "  signature.path is an absolute route; {name} and :name parameters are equivalent",
        "  signature.path parameters match request.params.properties exactly",
        "  API IDs, signature.name and each method+path pair are unique; one parameter position compares equally",
        "  one create per entity: a second POST for an entity another API already creates must reuse that API",
        "  a create keeps every required body field and every persisted column of the accepted create it replaces",
        "  when the accepted create is the narrower one, the new requirement extends it instead of adding a route",
        "  every required request.body value declares a type the compiler-owned HTTP flow can send",
        "  every requirements key exists in requirements.json and every API is a requirement's API",
        "  request.params/query/body/headers and response.success/errors are object schemas",
        "  success branches use 2xx and rejection branches use 4xx or 5xx"])
    return "\n".join(lines)


def concretizable(schema, depth: int = 0) -> bool:
    """Report whether a required request value can be built without inventing business data.

    The HTTP flow derives its success payload from the contract; a schema that names no
    type and no value keyword gives it nothing to send, so that API's success path could
    never be verified and the design would only ever be exercised on its error paths.
    """
    if not isinstance(schema, dict) or depth > 4:
        return False
    if any(keyword in schema for keyword in VALUE_KEYWORDS):
        return True
    declared = schema.get("type")
    kinds = [kind for kind in (declared if isinstance(declared, list) else [declared]) if isinstance(kind, str)]
    if any(kind in SYNTHESIZABLE_KINDS for kind in kinds):
        return True
    if "object" in kinds:
        properties = schema.get("properties") or {}
        required = [name for name in (schema.get("required") or []) if isinstance(name, str)]
        # An object that declares no property carries no value the flow could send, so a
        # required one is as unsendable as a schema naming no type at all.
        return bool(properties) and all(concretizable(properties.get(name), depth + 1) for name in required)
    if "array" in kinds:
        return concretizable(schema.get("items"), depth + 1)
    return False


def creates_by_entity(design) -> dict:
    """Map each entity to the API that creates it, so a design reuses it instead of cloning.

    The entity a ``POST`` creates is the one it writes the most columns of, so a
    registration that also opens a session creates the account while a sign-in that only
    writes a session creates the session. One rule, one answer: the planner, the contract
    checker and the design prompt all read this instead of guessing again.
    """
    creates: dict[str, str] = {}
    apis = design.get("apis") if isinstance(design, dict) else None
    for api_id, api in sorted((apis or {}).items()):
        signature = api.get("signature") if isinstance(api, dict) else None
        written = api.get("write") if isinstance(api, dict) else None
        if not isinstance(signature, dict) or not isinstance(written, list):
            continue
        if str(signature.get("method", "")).upper() != "POST":
            continue
        columns: dict[str, int] = {}
        for symbol in written:
            if isinstance(symbol, str) and QUALIFIED_FIELD.match(symbol):
                entity = symbol.split(".", 1)[0]
                columns[entity] = columns.get(entity, 0) + 1
        if columns:
            creates.setdefault(sorted(columns, key=lambda name: (-columns[name], name))[0], api_id)
    return creates


def requirement_traceability(design) -> dict:
    """Read the many-to-many Requirement -> API edges in both directions.

    One API may serve several requirements and one requirement may need several APIs. The
    design stores that relation once, in ``requirements``; this view exposes it as a trace
    so reuse is visible instead of being hidden inside a single requirement's list.
    """
    apis = design.get("apis") if isinstance(design, dict) else None
    requirements = design.get("requirements") if isinstance(design, dict) else None
    requirements = requirements if isinstance(requirements, dict) else {}
    by_api = {str(api_id): [] for api_id in apis} if isinstance(apis, dict) else {}
    for rid, api_ids in requirements.items():
        for api_id in api_ids if isinstance(api_ids, list) else []:
            if isinstance(api_id, str):
                by_api.setdefault(api_id, []).append(str(rid))
    return {"requirements": {str(rid): [item for item in ids if isinstance(item, str)]
            for rid, ids in requirements.items() if isinstance(ids, list)}, "apis": by_api}


def format_traceability_summary(design) -> str:
    """Render the Requirement <-> API trace, naming the APIs several requirements share."""
    trace = requirement_traceability(design)
    lines = [f"TRACEABILITY {len(trace['requirements'])} REQUIREMENT(S) -> {len(trace['apis'])} API(S)"]
    for rid, api_ids in trace["requirements"].items():
        lines.append(f"  {rid:<20} -> " + (", ".join(api_ids) if api_ids else "(no API)"))
    shared = {api_id: rids for api_id, rids in trace["apis"].items() if len(rids) > 1}
    if shared:
        lines.append("  REUSED APIS (many-to-many edges)")
        lines.extend(f"    {api_id:<30} <- " + ", ".join(rids) for api_id, rids in shared.items())
    return "\n".join(lines)



class _Checker:
    """Validate one design, collecting every problem instead of stopping at the first."""

    def __init__(self, known: set | None) -> None:
        """Remember the allowed requirement IDs and the cross-API identity registries."""
        self.problems: list[dict] = []
        self.known = known
        self.functions: dict[str, str] = {}
        self.routes: dict[tuple, tuple] = {}
        self.identifiers: dict[str, str] = {}
        # Every create operation, in declaration order, so a second business goal for one
        # entity is caught after the whole design is known.
        self.created: list[dict] = []

    def report(self, api_id, field: str, reason: str, fix: str) -> None:
        """Record one problem in the shape used for console reports and repair feedback."""
        self.problems.append({"api_id": api_id, "field": field, "reason": reason, "fix": fix})

    def design(self, design) -> list[dict]:
        """Check the artifact shape, every API, then the requirement associations."""
        if not isinstance(design, dict):
            self.report("", "design", f"the API design must be a JSON object, not {type(design).__name__}",
                'Return {"apis": {...}, "requirements": {...}}.')
            return self.problems
        apis = design.get("apis")
        if not isinstance(apis, dict):
            self.report("", "apis", f"'apis' must be an object keyed by API ID, not {type(apis).__name__}",
                'Return every designed API under "apis", or {} when no requirement needs an operation.')
            return self.problems
        # An empty map is legal only for a design with no operations at all: a requirement
        # that needs one is already rejected by the read/write obligation check.
        for api_id, api in apis.items():
            self.api(api_id, api)
        # Semantic reuse needs the whole design: one entity, one create business goal.
        self.reuse()
        self.requirements(design.get("requirements", {}), apis)
        return self.problems

    def api(self, api_id, api) -> None:
        """Check one API's identity, obligations, signature, I/O schemas and conditions."""
        if not isinstance(api_id, str) or not API_IDENTIFIER.match(api_id):
            self.report(str(api_id), "api_id", f"{api_id!r} is not a usable API ID",
                "Start with a letter or digit and use only letters, digits, '.', '_' or '-' so the ID can name a generated test file.")
        else:
            folded = api_id.lower()
            if folded in self.identifiers:
                self.report(api_id, "api_id", f"API ID {api_id!r} differs only in case from {self.identifiers[folded]!r}",
                    "Give each API a uniquely spelled ID; IDs are compared case-insensitively.")
            else:
                self.identifiers[folded] = api_id
        if not isinstance(api, dict):
            self.report(api_id, "api", f"the API definition must be an object, not {type(api).__name__}",
                "Return every documented property for this API.")
            return
        for key, description in API_PROPERTIES.items():
            if key not in api:
                self.report(api_id, key, f"required property {key!r} is missing",
                    f"Add {key!r}: {description}.")
        for mode in ("read", "write"):
            if mode in api:
                self.fields(api_id, mode, api[mode])
        path_names = self.signature(api_id, api["signature"]) if "signature" in api else None
        if "request" in api:
            self.request(api_id, api["request"], path_names)
        if "response" in api:
            self.response(api_id, api["response"])
        for key, status_field in (("precondition", "error_status"), ("postcondition", "status")):
            if key in api:
                self.conditions(api_id, key, api[key], status_field)
        self.created_api(api_id, api)

    def created_api(self, api_id, api) -> None:
        """Register one create operation so a second create for the same entity is caught.

        A ``POST`` that writes an entity creates that entity. The entity an operation
        creates is the one it writes the most columns of, so a registration that also opens
        a session is a create of the account, while a sign-in that only writes a session is
        a create of the session: the two are different business goals, not duplicates.
        """
        signature = api.get("signature") if isinstance(api, dict) else None
        written = api.get("write") if isinstance(api, dict) else None
        if not isinstance(signature, dict) or str(signature.get("method", "")).upper() != "POST":
            return
        if not isinstance(written, list):
            return
        columns: dict[str, set] = {}
        for symbol in written:
            if isinstance(symbol, str) and QUALIFIED_FIELD.match(symbol):
                entity, column = symbol.split(".", 1)
                columns.setdefault(entity, set()).add(column)
        if not columns:
            return
        entity = sorted(columns, key=lambda name: (-len(columns[name]), name))[0]
        body = (api.get("request") or {}).get("body") or {}
        properties = body.get("properties") or {}
        required = [name for name in (body.get("required") or []) if name in properties]
        self.created.append({"api_id": api_id, "entity": entity, "path": str(signature.get("path", "")),
            "columns": columns[entity], "required": required,
            "optional": [name for name in properties if name not in required]})

    def reuse(self) -> None:
        """Reject a second create for one entity and any create that drops its constraints.

        Reusing an accepted API keeps one handler, one contract and one set of business
        constraints; adding a second create for the same entity splits them, and the
        narrower of the two lets a caller or a test skip fields the requirement demands.
        The pair is rejected whichever order the two requirements arrive in: when this
        proposal is the wider one, consolidating has to widen the accepted create instead
        of leaving a narrow route behind that a client would have to fill in itself.
        """
        for index, current in enumerate(self.created):
            earlier = [item for item in self.created[:index] if item["entity"] == current["entity"]]
            # A shared required field proves both operations are the same business goal; a
            # session that one API opens for a new account and another opens for an existing
            # account shares no field, so those two creates stay distinct.
            shared = [item for item in earlier if set(item["required"]) & set(current["required"])]
            if not shared:
                continue
            first = max(shared, key=lambda item: (len(item["required"]), len(item["columns"])))
            entity = current["entity"]
            self.report(current["api_id"], "signature.method",
                f"{current['api_id']!r} is a second create for {entity}: {first['api_id']!r} already creates "
                f"{entity} at {first['path']!r}",
                f"Do not add a second create operation for {entity}. Reuse {first['api_id']!r} by listing that "
                f"API ID in requirement_apis for the requirement that needs the same business goal, and keep "
                f"{first['api_id']!r} the only route that creates {entity}.")
            # The duplicate is removed in both directions; only the direction decides which
            # create has to grow. When this proposal is the wider one, consolidating must
            # widen the accepted create, otherwise the requirement keeps only the narrower
            # route and the identity fields it demands would be invented by a caller or a test.
            extra = sorted(set(current["required"]) - set(first["required"]) - set(first["optional"]))
            columns = sorted(current["columns"] - first["columns"])
            if extra or columns:
                grows = ([f"its required body also carries {', '.join(extra)}"] if extra else []) \
                    + ([f"it persists {', '.join(f'{entity}.{column}' for column in columns)}"] if columns else [])
                invented = ", ".join(extra) or ", ".join(f"{entity}.{column}" for column in columns)
                self.report(current["api_id"], "request.body",
                    f"{current['api_id']!r} accepts {', '.join(sorted(current['required'])) or 'no body field'} "
                    f"but {first['api_id']!r} already creates {entity} with "
                    f"{', '.join(sorted(first['required'])) or 'no body field'}"
                    + (f" and persists only "
                       f"{', '.join(f'{entity}.{column}' for column in sorted(first['columns']))}"
                       if columns else ""),
                    f"Do not keep {current['api_id']!r} as a second route for {entity}. Extend "
                    f"{first['api_id']!r} instead so " + " and ".join(grows) + ", then let the requirement "
                    f"that needs them list {first['api_id']!r} in requirement_apis. No client, test or "
                    f"frontend may fabricate {invented} to make {first['api_id']!r} cover them.")
            dropped = sorted(set(first["required"]) - set(current["required"]) - set(current["optional"]))
            optional = sorted(set(first["required"]) & set(current["optional"]))
            fewer = sorted(first["columns"] - current["columns"])
            if not (dropped or optional or fewer):
                continue
            bypass = []
            if dropped:
                bypass.append("does not accept " + ", ".join(dropped))
            if optional:
                bypass.append("accepts " + ", ".join(optional) + " as optional")
            if fewer:
                bypass.append("persists only " + ", ".join(f"{entity}.{column}" for column in fewer))
            missing = ", ".join(dropped + optional)
            fabricated = (f"no client, test or frontend may invent {missing} to make it succeed"
                if missing else "no caller may skip the record detail it persists")
            self.report(current["api_id"], "request.body",
                f"{first['api_id']!r} requires {', '.join(first['required']) or 'no body field'}; "
                f"{current['api_id']!r} creates {entity} but " + "; ".join(bypass),
                f"Reuse {first['api_id']!r} so every business constraint stays enforced: its required body "
                f"fields and its persisted {entity} columns. A narrower create bypasses what another "
                f"requirement established, and {fabricated}.")


    def fields(self, api_id, mode: str, value) -> None:
        """Require a read or write set of qualified schema symbols."""
        if not isinstance(value, list):
            self.report(api_id, mode, f"{mode!r} must be an array of qualified DB fields, not {type(value).__name__}",
                'Use ["Entity.field"] with the exact schema names, or [] when the mode is unused.')
            return
        for symbol in value:
            if not isinstance(symbol, str) or not QUALIFIED_FIELD.match(symbol):
                self.report(api_id, mode, f"{symbol!r} is not a qualified DB field",
                    'Use the "Entity.field" spelling from db_schema.json, for example "Note.text".')

    def signature(self, api_id, signature) -> list[str] | None:
        """Check the handler name, HTTP method and route, and return the route parameters."""
        if not isinstance(signature, dict):
            self.report(api_id, "signature", f"the signature must be an object, not {type(signature).__name__}",
                'Use {"name": "listNotes", "method": "GET", "path": "/api/notes"}.')
            return None
        name = signature.get("name")
        if not isinstance(name, str) or not IDENTIFIER.match(name):
            self.report(api_id, "signature.name", f"{name!r} is not a valid TypeScript function name",
                "Use an identifier such as listNotes; the generated registration imports it by name.")
        elif name in self.functions:
            self.report(api_id, "signature.name", f"function name {name!r} is already used by {self.functions[name]!r}",
                "Give each API a distinct signature.name; one generated module cannot export the same name twice.")
        else:
            self.functions[name] = api_id
        method = signature.get("method")
        if not isinstance(method, str) or method.upper() not in METHODS:
            self.report(api_id, "signature.method", f"{method!r} is not a supported HTTP method",
                "Use one of " + ", ".join(METHODS) + ".")
            method = None
        path = signature.get("path")
        names = self.path(api_id, path)
        if method is not None and names is not None:
            self.route(api_id, method.upper(), path)
        return names

    def path(self, api_id, path) -> list[str] | None:
        """Require an absolute route with unique, well-formed {name} or :name parameters."""
        field = "signature.path"
        if not isinstance(path, str) or not path:
            self.report(api_id, field, f"{path!r} is not a path string",
                'Use an absolute route such as "/api/books/{bookTitle}".')
            return None
        if not path.startswith("/") or path.rstrip("/") == "":
            self.report(api_id, field, f"{path!r} is not an absolute path below /",
                'Start the path with "/" and name a resource, for example "/api/books".')
            return None
        if any(character.isspace() for character in path) or any(character in FORBIDDEN_PATH_CHARACTERS for character in path):
            self.report(api_id, field, f"{path!r} contains whitespace, a query, a fragment or an escape character",
                'Use plain path segments only; put filters in request.query and describe segments as "{name}" parameters.')
            return None
        if "//" in path:
            self.report(api_id, field, f"{path!r} contains an empty segment", "Remove the duplicated '/'.")
            return None
        names = [match.group(1) or match.group(2) for match in PARAMETER.finditer(path)]
        remainder = PARAMETER.sub("", path)
        if BRACES.search(remainder) or DANGLING.search(remainder):
            self.report(api_id, field, f"{path!r} has an unbalanced or nameless route parameter",
                'Write "{bookTitle}" or ":bookTitle"; do not leave a dangling "{" or ":".')
            return None
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            self.report(api_id, field, f"{path!r} repeats the route parameter(s) {', '.join(repeated)}",
                "Give each parameter position its own distinct name.")
            return None
        return names

    def route(self, api_id, method: str, path: str) -> None:
        """Reject a duplicate or ambiguous method+path pair; parameter names compare by position."""
        key = (method, PARAMETER.sub("*", path.rstrip("/")))
        previous = self.routes.get(key)
        if previous is None:
            self.routes[key] = (api_id, path)
            return
        other_id, other_path = previous
        reason = (f"{method} {path!r} duplicates {other_id!r}" if other_path.rstrip("/") == path.rstrip("/")
            else f"{method} {path!r} is ambiguous with {other_id!r} ({other_path!r}): both routes match the same URLs")
        self.report(api_id, "signature.path", reason,
            "Keep one method+path per operation and reuse the existing API ID instead of adding a second route.")

    def request(self, api_id, request, path_names) -> None:
        """Require four object schemas and make request.params agree with the route."""
        if not isinstance(request, dict):
            self.report(api_id, "request", f"request must be an object, not {type(request).__name__}",
                'Use {"params": {...}, "query": {...}, "body": {...}, "headers": {...}} with object schemas.')
            return
        declared = None
        for part in REQUEST_PARTS:
            properties = self.schema(api_id, f"request.{part}", request.get(part))
            if part == "params" and properties is not None:
                declared = list(properties)
            if part == "body" and properties is not None:
                self.payload(api_id, request.get(part) or {})
        if declared is None or path_names is None:
            return
        undeclared = [name for name in path_names if name not in declared]
        unused = [name for name in declared if name not in path_names]
        if undeclared:
            self.report(api_id, "request.params",
                f"route parameter(s) {', '.join(undeclared)} are missing from request.params.properties",
                'Declare every "{name}" of signature.path under request.params.properties as {"type": "string"}.')
        if unused:
            self.report(api_id, "request.params",
                f"request.params declares {', '.join(unused)} which signature.path never supplies",
                "Remove the unused declaration or add the matching path segment.")

    def payload(self, api_id, body) -> None:
        """Require a required request body value the compiler-owned HTTP flow can send.

        Step 08 proves the success flow over real HTTP, so a required value no generator can
        build would leave that API verified only on its rejection paths. Declaring a concrete
        type keeps the success path provable without inventing business data.
        """
        properties = body.get("properties") or {}
        for name in body.get("required") or []:
            if isinstance(name, str) and name in properties and not concretizable(properties[name]):
                self.report(api_id, f"request.body.properties.{name}",
                    f"the required body value {name!r} declares no value the HTTP flow can send",
                    'Declare "type": "string", "number", "integer", "boolean", an object whose required '
                    "properties declare such a type, or a const/default/examples/enum value; otherwise make "
                    "the property optional so a complete success payload can still be built.")

    def schema(self, api_id, field: str, schema):
        """Accept any legal JSON Schema keywords but require an object schema shape."""
        if not isinstance(schema, dict):
            self.report(api_id, field, f"the schema must be a JSON object, not {type(schema).__name__}",
                'Use {"type": "object"} when this part carries no data.')
            return None
        kind = schema.get("type")
        if kind is not None and kind != "object":
            self.report(api_id, field, f"the schema type must be 'object', not {kind!r}",
                'Use {"type": "object", "properties": {...}}; nested values declare their own type.')
            return None
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            self.report(api_id, field + ".properties", f"'properties' must be an object, not {type(properties).__name__}",
                "Map each property name to its inline JSON schema.")
            return None
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            self.report(api_id, field + ".required", "'required' must be an array of property names",
                'Use ["text"] and keep every name declared in properties.')
        else:
            unknown = [item for item in required if item not in properties]
            if unknown:
                self.report(api_id, field + ".required", f"{', '.join(unknown)} are not declared in properties",
                    "Declare each required property or remove it from 'required'.")
        return properties

    def response(self, api_id, response) -> None:
        """Require a 2xx success branch and 4xx/5xx rejection branches with body and headers."""
        if not isinstance(response, dict):
            self.report(api_id, "response", f"response must be an object, not {type(response).__name__}",
                'Use {"success": {"status": 200, "body": {...}, "headers": {...}}, "errors": []}.')
            return
        success = response.get("success")
        if not isinstance(success, dict):
            self.report(api_id, "response.success", f"the success branch must be an object, not {type(success).__name__}",
                'Add {"status": 200, "body": {...}, "headers": {...}}: the handler always answers through it.')
        else:
            self.status(api_id, "response.success.status", success.get("status"), (200, 299), "a 2xx success status")
            for part in RESPONSE_PARTS:
                self.schema(api_id, f"response.success.{part}", success.get(part))
        errors = response.get("errors", [])
        if not isinstance(errors, list):
            self.report(api_id, "response.errors", f"'errors' must be an array, not {type(errors).__name__}",
                "Use [] when the operation cannot fail, otherwise one branch per rejection.")
            return
        for index, error in enumerate(errors):
            field = f"response.errors[{index}]"
            if not isinstance(error, dict):
                self.report(api_id, field, f"each error branch must be an object, not {type(error).__name__}",
                    'Use {"status": 400, "body": {...}, "headers": {...}}.')
                continue
            self.status(api_id, field + ".status", error.get("status"), (400, 599), "a 4xx or 5xx rejection status")
            for part in RESPONSE_PARTS:
                self.schema(api_id, f"{field}.{part}", error.get(part))

    def status(self, api_id, field: str, value, bounds: tuple, label: str) -> None:
        """Accept an integer inside the range documented for this branch."""
        if not isinstance(value, int) or isinstance(value, bool) or not bounds[0] <= value <= bounds[1]:
            self.report(api_id, field, f"{value!r} is not {label}",
                f"Use an integer between {bounds[0]} and {bounds[1]}.")

    def conditions(self, api_id, key: str, entries, status_field: str) -> None:
        """Check the documented P/Q entry shape without judging its business meaning."""
        if not isinstance(entries, list):
            self.report(api_id, key, f"{key!r} must be an array, not {type(entries).__name__}",
                "Use [] when the API has no such condition.")
            return
        for index, entry in enumerate(entries):
            field = f"{key}[{index}]"
            if not isinstance(entry, dict):
                self.report(api_id, field, f"each entry must be an object, not {type(entry).__name__}",
                    f'Use {{"id": "...", "description": "...", "{status_field}": 400}}.')
                continue
            if not isinstance(entry.get("id"), str) or not entry["id"].strip():
                self.report(api_id, field + ".id", "the entry needs a non-empty string id",
                    "Use a stable id such as P-nonempty-text.")
            if not isinstance(entry.get("description"), str) or not entry["description"].strip():
                self.report(api_id, field + ".description", "the entry needs a description of the observable outcome",
                    "Describe the condition and the response or database effect it produces.")
            value = entry.get(status_field)
            if value is not None:
                bounds = (400, 599) if status_field == "error_status" else (200, 599)
                self.status(api_id, f"{field}.{status_field}", value, bounds, "an integer HTTP status")

    def requirements(self, requirements, apis: dict) -> None:
        """Require existing requirement IDs and a requirement that references every API."""
        if not isinstance(requirements, dict):
            self.report("", "requirements", f"'requirements' must be an object, not {type(requirements).__name__}",
                'Map each requirement ID to the APIs it uses, for example {"REQ-1": ["API-list-notes"]}.')
            return
        used = set()
        for rid, api_ids in requirements.items():
            field = f"requirements.{rid}"
            if self.known is not None and rid not in self.known:
                self.report(str(rid), field, f"requirement {rid!r} does not exist in requirements.json",
                    "Reference only requirement IDs produced by step 01; do not invent requirements.")
            if not isinstance(api_ids, list) or not all(isinstance(item, str) for item in api_ids):
                self.report(str(rid), field, "the value must be an array of API IDs",
                    'Use [] for requirements without backend behavior.')
                continue
            for api_id in api_ids:
                if api_id not in apis:
                    self.report(str(rid), field, f"{api_id!r} is not defined in apis",
                        "List only API IDs this design defines, and add the missing definition.")
                else:
                    used.add(api_id)
        for api_id in apis:
            if api_id not in used:
                self.report(str(api_id), "requirements", "no requirement lists this API",
                    "Add the API ID to the requirements that use it, or remove the unused API.")

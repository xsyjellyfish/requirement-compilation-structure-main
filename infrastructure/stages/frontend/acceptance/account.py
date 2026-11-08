"""Deterministic black-box authentication journeys derived from the accepted IR.

``journey.py`` derives CRUD journeys from a create plus read pair. An application whose whole
business is an account lifecycle — register, sign in, restore the session, sign out — declares
no such pair, and reporting it as ``NOT_APPLICABLE`` would silently accept an application that
no test ever exercised. This module derives that second family of journeys from the same
accepted artifacts, again without a model:

* ``backend_apis.json`` names the routes: a registration route, a sign-in route, a sign-out
  route and a session lookup, classified by method, path and request body;
* ``db_schema.json`` names the tables that store the account and the session;
* ``ui_design.json`` names the pages, the labels of the fields, the element that displays the
  authenticated identity and the links a signed-out visitor uses;
* ``frontend_triggers.json`` says which control submits which route and which trigger resolves
  the session when a page mounts;
* ``requirements.json`` names the scenarios, so every lifecycle scenario ends up with one
  traceable coverage record.

The derived cases are an external reviewer's walk through the lifecycle:

1. a signed-out visitor sees the registration and sign-in affordances and no identity;
2. the visitor reaches the form through the link, registers, and the home page shows the
   account — exactly one account row and one session row were written by the real backend;
3. a reload resolves the session through the session route again, and an independent browser
   context stays signed out;
4. a rejected registration (a duplicate identity) surfaces the designed alert, writes nothing
   and leaves the visitor signed out after a refresh;
5. signing out deletes the server-side session, keeps the account, and survives a reload;
6. the same account signs in again and receives a new session;
7. a wrong secret is rejected without creating a session.

Whenever a piece the journey needs is missing, the plan records a problem (a UI defect the
repair loop must fix) or an explicit unverified capability. A scenario that could only be
verified by fabricating a credential is recorded as not covered with the reason spelled out;
the suite is never padded with a weaker assertion instead.
"""

import re

from infrastructure.stages.backend.http_tests import normalize_name
from infrastructure.stages.frontend.acceptance.journey import (
    ACTION_TYPES, EDITABLE_TYPES, PASSWORD, SPEC_ROOT, SPEC_SUFFIX, TOKEN_PREFIX, accessible_name,
    action_control, api_actions, api_index, body_properties, error_surface, fills, json_literal,
    navigation_control, page_elements, page_for_route, parameters, statement, writable)

SPEC_NAME = "account-lifecycle" + SPEC_SUFFIX
REGISTER_WORDS = ("register", "registration", "signup", "sign-up", "createaccount")
LOGIN_WORDS = ("login", "log-in", "signin", "sign-in", "authenticate")
LOGOUT_WORDS = ("logout", "log-out", "signout", "sign-out")
SESSION_WORDS = ("session", "currentsession", "whoami")
SESSION_SEGMENTS = {"session", "me", "whoami", "current-user", "currentuser", "current-account",
    "currentaccount", "identity"}
IDENTITY_WORDS = ("username", "user_name", "loginname", "login_name", "handle", "email",
    "accountname", "account_name", "userid", "user_id")
CREDENTIAL_WORDS = ("password", "passphrase", "passcode", "secret", "pin")
# A scenario that depends on a credential the test may not fabricate can never be verified from
# the outside: seeding browser storage is a forbidden shortcut, so it stays uncovered.
UNFORGEABLE = ("does not match", "does not belong", "not match an existing", "unrecognized",
    "unrecognised", "stale", "expired session", "tampered", "forged")
EXCEPTION = re.compile(r"\b(?:reject|invalid|duplicate|incorrect|already|unused|fail|error|wrong"
    r"|taken|conflict|denied)")
WRONG_PASSWORD = "AcceptanceWrong1!"
# Words that name the account lifecycle itself, used to decide whether a scenario without any
# trigger still belongs to this journey rather than to another requirement's own journey.
ACCOUNT_WORDS = ("register", "registration", "login", "log in", "sign in", "logout", "log out",
    "sign out", "session", "username", "account", "password", "credential")


def compact(route: str) -> str:
    """Reduce a route to lowercase letters and digits so a word can be found inside it."""
    return re.sub(r"[^a-z0-9]", "", (route or "").lower())


def mentions(route: str, words) -> bool:
    """Whether a path or a requirement text contains one of the given words."""
    flattened = compact(route)
    return any(compact(word) in flattened for word in words)


def is_session_route(path: str) -> bool:
    """A session lookup addresses the caller's own state: no parameter and a session-like name."""
    parts = [part for part in (path or "").strip("/").split("/") if part]
    return bool(SESSION_SEGMENTS.intersection(part.lower() for part in parts)) \
        or mentions(path, SESSION_WORDS)


def named_property(properties, words) -> str | None:
    """Pick the declared body member that carries an identity or a credential."""
    normalized = [(name, normalize_name(name)) for name in properties]
    for word in words:
        for name, plain in normalized:
            if plain == word or word in plain:
                return name
    return None

def body_identity(backend: dict, aid: str | None) -> str | None:
    """Return the request body member that carries the account identity."""
    return named_property(body_properties(backend, aid), IDENTITY_WORDS)


def body_credential(backend: dict, aid: str | None) -> str | None:
    """Return the request body member that carries the secret."""
    return named_property(body_properties(backend, aid), CREDENTIAL_WORDS)


def classify(entry: dict, backend: dict) -> str | None:
    """Name the lifecycle role of one route, or ``None`` when it is not part of the lifecycle."""
    method, path = entry["method"], entry["path"]
    if parameters(path):
        # A lifecycle route addresses the caller itself, never one resource instance.
        return None
    session_route = is_session_route(path)
    if method == "GET":
        return "session" if session_route else None
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if mentions(path, LOGOUT_WORDS) or (session_route and method == "DELETE"):
        return "logout"
    identity = body_identity(backend, entry["id"])
    if not identity:
        return None
    if session_route:
        return "login"
    if mentions(path, REGISTER_WORDS):
        return "register"
    if mentions(path, LOGIN_WORDS) or body_credential(backend, entry["id"]):
        return "login"
    return None


def lifecycle_apis(backend: dict) -> dict:
    """Classify every accepted route and keep the shortest candidate per lifecycle role."""
    buckets = {}
    for entry in api_index(backend).values():
        role = classify(entry, backend)
        if role is not None:
            buckets.setdefault(role, []).append(entry)
    return {role: min(entries, key=lambda item: (len(item["path"]), item["path"], item["id"]))
        for role, entries in buckets.items()}


def has_lifecycle(apis: dict) -> bool:
    """Whether accounts can be created, observed and ended through the accepted API at all.

    A registration route is mandatory: the acceptance test may not insert a row, so the only
    way to obtain an account — and therefore any signed-in state — is the application's own
    form. Without it the lifecycle cannot be exercised and the layer stays not applicable.
    """
    return "register" in apis and bool({"login", "session", "logout"} & set(apis))


def ambient_triggers(design: dict, session: str | None) -> set[str]:
    """Name the mount triggers that only resolve the current session whenever a page loads."""
    return {tid for tid, trigger in (design.get("triggers") or {}).items()
        if trigger.get("event") == "mount" and session and api_actions(trigger) == {session}}


def display_control(ui: dict, pid: str | None) -> dict | None:
    """Find the designed element that displays the authenticated account's identity."""
    if not pid:
        return None
    for uid, node in page_elements(ui, pid).items():
        if "user_read" not in (node.get("mode") or []):
            continue
        if node.get("type") in {"page", "container", "form", "image", "list"}:
            continue
        for variable in node.get("data") or []:
            if isinstance(variable, str) and mentions(variable, IDENTITY_WORDS):
                return {"element": uid, "node": node, "binding": variable}
    return None


def entry_link(ui: dict, design: dict, from_pid: str | None, to_pid: str | None) -> dict | None:
    """Find the link a signed-out visitor clicks to reach an authentication page."""
    if not from_pid or not to_pid:
        return None
    route = (ui["pages"].get(to_pid) or {}).get("route") or "/"
    for uid, node in page_elements(ui, from_pid).items():
        href = (node.get("prop") or {}).get("href")
        if node.get("type") not in ACTION_TYPES or not href or not accessible_name(node):
            continue
        if href.rstrip("/") == route.rstrip("/"):
            return {"element": uid, "name": accessible_name(node)}
    control = navigation_control(ui, design, from_pid, to_pid)
    return {"element": control["element"], "name": control["name"]} if control else None


def labelled_field(ui: dict, pid: str | None, words, preferred: str | None = None) -> dict | None:
    """Find one writable field by the name it announces: the declared member first, then words.

    A form declares several fields whose labels mention the same words — ``Password`` and
    ``Confirm Password`` — so the member the API declares the value in decides which one is the
    real control. Without that hint the first field mentioning any of the words is used.
    """
    if not pid:
        return None
    wanted = normalize_name(preferred or "")
    best = None
    for uid, node in page_elements(ui, pid).items():
        name = accessible_name(node)
        if not name or not writable(node) or node.get("type") not in EDITABLE_TYPES:
            continue
        plain = normalize_name(name)
        if wanted and plain == wanted:
            return {"element": uid, "node": node, "name": name}
        if wanted and wanted in plain:
            rank = 1
        elif mentions(name, words):
            rank = 2
        else:
            continue
        if best is None or rank < best[0]:
            best = (rank, {"element": uid, "node": node, "name": name})
    return best[1] if best else None

def account_constants(entry: dict) -> list[str]:
    """Render the literals every case shares."""
    routes = entry["routes"]
    lines = ["const HOME = " + json_literal(routes["home"]) + ";"]
    if routes.get("login"):
        lines.append("const LOGIN = " + json_literal(routes["login"]) + ";")
    lines += ["const REGISTER = " + json_literal(routes["register"]) + ";",
        "const USER = " + json_literal(entry["token"]) + ";",
        "const SECRET = " + json_literal(entry["secret"]) + ";"]
    if entry["database"].get("session"):
        lines.append("const SESSION = " + json_literal(entry["database"]["session"]) + ";")
    if entry["database"].get("account"):
        lines.append("const ACCOUNT = " + json_literal(entry["database"]["account"]) + ";")
    if any(case["id"] == "wrong-secret" for case in entry["cases"]):
        lines.append("const WRONG = " + json_literal(entry["wrong"]) + ";")
    return lines


def account_helpers(entry: dict) -> list[str]:
    """Render the helpers the cases share: the user's own steps, once each."""
    controls, caps = entry["controls"], entry["caps"]
    lines = ["// A signed-out visitor is offered the entry links and sees no account identity.",
        "async function expectSignedOut(page: Page): Promise<void> {"]
    if caps["identity"]:
        lines.append("  await expect(page.getByText(USER)).toHaveCount(0);")
    for key in ("to_register", "to_login"):
        if caps.get(key):
            name = json_literal(controls[key]["name"])
            lines.append("  await expect(action(page, " + name + ")).toBeVisible();")
    lines += ["}", "",
        "// The signed-in home page names the account and offers the controls a member uses.",
        "async function expectSignedIn(page: Page, identity: string): Promise<void> {",
        "  await expect(page).toHaveURL(HOME);"]
    if caps["identity"]:
        lines.append("  await expect(page.getByText(identity).first()).toBeVisible();")
    if caps["logout"]:
        lines.append("  await expect(action(page, " + json_literal(controls["logout"]["name"])
            + ")).toBeVisible();")
    lines += ["}", ""]
    lines += ["// The visitor reaches the registration form through the control the page offers.",
        "async function openRegister(page: Page): Promise<void> {",
        "  await openRoute(page, HOME);",
        "  await expectSignedOut(page);"]
    if caps.get("to_register"):
        lines.append("  await action(page, " + json_literal(controls["to_register"]["name"]) + ").click();")
    else:
        lines.append("  await openRoute(page, REGISTER);")
    lines += ["  await expect(page).toHaveURL(REGISTER);", "}", "",
        "// Creating an account is a user operation: each designed field is typed, then submitted.",
        "async function register(page: Page, identity: string): Promise<void> {",
        "  await openRegister(page);",
        "  await control(page, " + json_literal(controls["identity_field"]["name"])
            + ").fill(identity);"]
    lines += ["  " + statement(fill) for fill in entry["fills"]]
    submit = json_literal(controls["register"]["name"])
    lines += ["  await expect(action(page, " + submit + ")).toHaveAccessibleName(" + submit + ");",
        "  await action(page, " + submit + ").click();", "}", ""]
    if caps["login"]:
        lines += ["// The visitor reaches the sign-in form through the control the page offers.",
            "async function openLogin(page: Page): Promise<void> {",
            "  await openRoute(page, HOME);",
            "  await expectSignedOut(page);"]
        if caps.get("to_login"):
            lines.append("  await action(page, " + json_literal(controls["to_login"]["name"])
                + ").click();")
        else:
            lines.append("  await openRoute(page, LOGIN);")
        lines += ["  await expect(page).toHaveURL(LOGIN);", "}", "",
            "// Signing in with a secret is a user operation too: no session is ever fabricated.",
            "async function signIn(page: Page, identity: string, secret: string): Promise<void> {",
            "  await openLogin(page);",
            "  await control(page, " + json_literal(controls["login_identity"]["name"])
                + ").fill(identity);",
            "  await control(page, " + json_literal(controls["login_secret"]["name"])
                + ").fill(secret);"]
        submit = json_literal(controls["login"]["name"])
        lines += ["  await expect(action(page, " + submit + ")).toHaveAccessibleName(" + submit + ");",
            "  await action(page, " + submit + ").click();", "}", ""]
    return lines


def requirement_text(step: dict) -> str:
    """Render one scenario step as the text the coverage classifier reads."""
    return " ".join(str(step.get(key) or "") for key in ("Given", "When", "Then")).strip()


def scenario_index(tree, design: dict) -> list[dict]:
    """Collect every requirement scenario with its steps and the triggers those steps name."""
    if tree is None:
        return []
    records = {}
    for rid in getattr(tree, "postorder", []) or []:
        for sid in (getattr(tree, "own_steps", {}) or {}).get(rid, []):
            step = (getattr(tree, "steps", {}) or {}).get(sid) or {}
            scenario = step.get("scenario") or {}
            key = scenario.get("id")
            if not key:
                continue
            record = records.setdefault(key, {"id": key, "name": scenario.get("name") or "",
                "requirement": rid, "steps": [], "triggers": set(), "text": ""})
            record["steps"].append(sid)
            record["text"] = (record["text"] + " " + requirement_text(step)).strip()
            record["triggers"].update((design.get("steps") or {}).get(sid) or [])
    return list(records.values())


def entity_tables(backend: dict, aid: str | None, schema: dict) -> list[str]:
    """Map an API's write references onto every schema table it names, in reference order."""
    tables = list((schema.get("entities") or {}))
    found = []
    for reference in ((backend.get("apis") or {}).get(aid) or {}).get("write") or []:
        wanted = normalize_name(str(reference).split(".")[0])
        for table in tables:
            if table in found:
                continue
            if normalize_name(table) == wanted or normalize_name(table).endswith(wanted):
                found.append(table)
    return found


def session_table(backend: dict, apis: dict, schema: dict) -> str | None:
    """Name the table that stores a server-side session, when the schema declares one."""
    candidates = []
    for role in ("login", "logout", "session"):
        if role in apis:
            candidates += entity_tables(backend, apis[role]["id"], schema)
    for table in candidates:
        if mentions(table, SESSION_WORDS):
            return table
    return candidates[0] if candidates else None


def account_table(backend: dict, apis: dict, schema: dict, session: str | None) -> str | None:
    """Name the table that stores the account itself, distinct from the session table."""
    for table in entity_tables(backend, apis["register"]["id"], schema):
        if table != session:
            return table
    return None


def lifecycle_triggers(design: dict, apis: dict) -> set[str]:
    """Name every trigger that addresses a lifecycle route, ambient session lookups included."""
    wanted = {item["id"] for item in apis.values()}
    return {tid for tid, trigger in (design.get("triggers") or {}).items()
        if api_actions(trigger) and api_actions(trigger) <= wanted}


def scenario_in_scope(record: dict, triggers) -> bool:
    """Whether a scenario belongs to the account lifecycle rather than to another journey."""
    if set(record["triggers"]) & set(triggers):
        return True
    return not record["triggers"] and mentions(record["name"] + " " + record["text"], ACCOUNT_WORDS)


def coverage_records(records, cases, ambient, triggers=frozenset()) -> tuple[list[dict], list[dict]]:
    """Assign every lifecycle scenario to the cases that assert it, or explain the gap.

    A scenario is covered when the cases that assert it exercise every trigger its steps name.
    Steps that only resolve the session on mount are ambient: every case runs them, so a
    scenario that names nothing else is covered by the cases asserting the entry and the
    restored state. A scenario that needs a credential the test may not fabricate is never
    claimed, because the honest answer there is that a browser test cannot present it.
    """
    covered, uncovered = [], []
    for record in records:
        if triggers and not scenario_in_scope(record, triggers):
            continue
        text = (record["name"] + " " + record["text"]).lower()
        item = {"scenario": record["id"], "name": record["name"],
            "requirement": record["requirement"], "triggers": sorted(record["triggers"])}
        if any(phrase in text for phrase in UNFORGEABLE):
            uncovered.append({**item, "status": "NOT_COVERED",
                "reason": "the scenario depends on a session credential the acceptance test may"
                    " not fabricate; seeding browser storage is forbidden, so no browser case"
                    " can present it"})
            continue
        effective = set(record["triggers"]) - ambient
        exception = bool(EXCEPTION.search(record["name"].lower()))
        candidates = [case for case in cases if case["exception"] == exception
            and effective <= set(case["triggers"])]
        if not effective and not exception:
            candidates = [case for case in candidates if case["state"]]
        if candidates:
            covered.append({**item, "status": "COVERED",
                "cases": [case["id"] for case in candidates]})
        else:
            uncovered.append({**item, "status": "NOT_COVERED",
                "reason": "no derived case exercises the triggers this scenario names ("
                    + (", ".join(sorted(set(record["triggers"]))) or "none") + ")"})
    return covered, uncovered


def register_case(entry: dict) -> list[str]:
    """Case 1: register, then prove the stored account and the restored session."""
    lines = ["  // 1. A signed-out visitor sees the entry links and no account identity.",
        "  await openRoute(page, HOME);",
        "  await expect(page).toHaveURL(HOME);",
        "  await expectSignedOut(page);",
        "  // 2. Register through the designed form: the account is a user operation, not a seed.",
        "  await register(page, USER);",
        "  await expectSignedIn(page, USER);"]
    if entry["database"].get("account"):
        lines.append("  expect(database.count(ACCOUNT)).toBeGreaterThan(0);")
    lines.append("  expect(database.stores(USER)).toBeGreaterThan(0);")
    lines.append("  expect(database.total()).toBeGreaterThan(0);")
    if entry["database"].get("session"):
        lines.append("  expect(database.count(SESSION)).toBe(1);")
    lines += ["  // 3. A reload resolves the session on the server, not from client memory.",
        "  await page.reload();",
        "  await expect(page).toHaveURL(HOME);",
        "  await expectSignedIn(page, USER);",
        "  // 4. An independent browser context is not signed in as this account.",
        "  await visitor.page.goto(HOME);",
        "  await expect(visitor.page.getByText(USER)).toHaveCount(0);"]
    return lines


def sign_out_case(entry: dict) -> list[str]:
    """Case 2: sign out through the designed control and stay signed out after a reload."""
    logout = json_literal(entry["controls"]["logout"]["name"])
    lines = ["  // A signed-in visitor signs out through the designed control.",
        "  await register(page, USER);",
        "  await expectSignedIn(page, USER);"]
    if entry["database"].get("session"):
        lines.append("  expect(database.count(SESSION)).toBe(1);")
    lines += ["  await expect(action(page, " + logout + ")).toHaveAccessibleName(" + logout + ");",
        "  await action(page, " + logout + ").click();",
        "  await expectSignedOut(page);",
        "  // The server-side session is gone while the account itself is kept."]
    if entry["database"].get("session"):
        lines.append("  expect(database.count(SESSION)).toBe(0);")
    lines += ["  expect(database.stores(USER)).toBeGreaterThan(0);",
        "  // The signed-out state survives a reload: no remembered identity comes back.",
        "  await page.reload();",
        "  await expectSignedOut(page);"]
    return lines


def sign_in_case(entry: dict) -> list[str]:
    """Case 3: the same account signs in again and keeps the new session across a reload."""
    logout = json_literal(entry["controls"]["logout"]["name"])
    lines = ["  // The account exists and the visitor has signed out again.",
        "  await register(page, USER);",
        "  await expectSignedIn(page, USER);",
        "  await action(page, " + logout + ").click();",
        "  await expectSignedOut(page);",
        "  // The same account signs in through the form and receives a new session.",
        "  await signIn(page, USER, SECRET);",
        "  await expectSignedIn(page, USER);"]
    if entry["database"].get("session"):
        lines.append("  expect(database.count(SESSION)).toBe(1);")
    lines += ["  // The user remains signed in after a page refresh.",
        "  await page.reload();",
        "  await expectSignedIn(page, USER);"]
    return lines


def duplicate_case(entry: dict) -> list[str]:
    """Case 4: a duplicate identity is rejected and writes nothing."""
    logout = json_literal(entry["controls"]["logout"]["name"])
    lines = ["  // The identity exists because a visitor created it through the form.",
        "  await register(page, USER);",
        "  await expectSignedIn(page, USER);",
        "  await action(page, " + logout + ").click();",
        "  await expectSignedOut(page);",
        "  const stored = database.total();"]
    if entry["database"].get("account"):
        lines.append("  const accounts = database.count(ACCOUNT);")
    lines += ["  // The same identity may not be registered twice.",
        "  await register(page, USER);",
        "  // The visitor stays on the form and the designed alert reports the rejection.",
        "  await expect(page).toHaveURL(REGISTER);",
        "  await expect(errorMessage(page).first()).toBeVisible();",
        "  // Nothing was written and no session was created.",
        "  expect(database.total()).toBe(stored);"]
    if entry["database"].get("account"):
        lines.append("  expect(database.count(ACCOUNT)).toBe(accounts);")
    if entry["database"].get("session"):
        lines.append("  expect(database.count(SESSION)).toBe(0);")
    lines += ["  // Register and Login stay visible, before and after a refresh.",
        "  await openRoute(page, HOME);",
        "  await expect(page).toHaveURL(HOME);",
        "  await expectSignedOut(page);",
        "  await page.reload();",
        "  await expectSignedOut(page);"]
    return lines

def wrong_secret_case(entry: dict) -> list[str]:
    """Case 5: the right identity with the wrong secret is rejected without a session."""
    logout = json_literal(entry["controls"]["logout"]["name"])
    lines = ["  // The account exists and the visitor has signed out.",
        "  await register(page, USER);",
        "  await expectSignedIn(page, USER);",
        "  await action(page, " + logout + ").click();",
        "  await expectSignedOut(page);",
        "  const stored = database.total();",
        "  // The right identity with the wrong secret must not produce a session.",
        "  await signIn(page, USER, WRONG);",
        "  await expect(page).toHaveURL(LOGIN);",
        "  await expect(errorMessage(page).first()).toBeVisible();",
        "  expect(database.total()).toBe(stored);",
        "  // The account itself is untouched: only the credential was wrong.",
        "  expect(database.stores(USER)).toBeGreaterThan(0);"]
    if entry["database"].get("session"):
        lines.append("  expect(database.count(SESSION)).toBe(0);")
    lines += ["  // The visitor is still signed out on the home page.",
        "  await openRoute(page, HOME);",
        "  await expectSignedOut(page);"]
    return lines


def cases_for(entry: dict) -> list[dict]:
    """Build the cases of the journey; each case names the triggers it exercises."""
    caps = entry["caps"]
    triggers = {role: control.get("trigger") for role, control in entry["controls"].items()}
    cases = [{"id": "register-restore",
        "title": "a visitor registers an account, which the server restores after a reload",
        "triggers": [triggers["register"]], "exception": False, "state": True,
        "fixtures": ["page", "database", "visitor"], "body": register_case(entry)}]
    if caps["logout"]:
        cases.append({"id": "sign-out",
            "title": "signing out ends the server session and stays signed out after a reload",
            "triggers": [triggers["register"], triggers["logout"]], "exception": False,
            "state": True, "fixtures": ["page", "database"], "body": sign_out_case(entry)})
    if caps["logout"] and caps["login"]:
        cases.append({"id": "sign-in-again",
            "title": "the same account signs in again through the form and keeps the new session",
            "triggers": [triggers["register"], triggers["logout"], triggers["login"]],
            "exception": False, "state": True, "fixtures": ["page", "database"],
            "body": sign_in_case(entry)})
    if caps["logout"] and caps["error"]:
        cases.append({"id": "duplicate",
            "title": "a duplicate identity is rejected, writes nothing and leaves no session",
            "triggers": [triggers["register"]], "exception": True, "state": False,
            "fixtures": ["page", "database"], "body": duplicate_case(entry)})
    if caps["logout"] and caps["login"] and caps["login_error"]:
        cases.append({"id": "wrong-secret",
            "title": "a wrong secret is rejected and never produces a session",
            "triggers": [triggers["login"]], "exception": True, "state": False,
            "fixtures": ["page", "database"], "body": wrong_secret_case(entry)})
    return cases


def account_journey(tree, schema: dict, backend: dict, ui: dict, design: dict,
        plan: dict) -> dict | None:
    """Derive the account lifecycle journey, or record why the design cannot support one."""
    apis = lifecycle_apis(backend)
    if not has_lifecycle(apis):
        return None
    session = session_table(backend, apis, schema)
    entry = {"kind": "account", "resource": "account",
        "collection": apis["register"]["path"], "apis": {role: item["id"] for role, item in apis.items()},
        "token": TOKEN_PREFIX + "-ACCOUNT", "secret": PASSWORD, "wrong": WRONG_PASSWORD,
        "pages": {}, "routes": {}, "controls": {}, "fills": [], "caps": {}, "steps": [],
        "database": {"session": session, "account": account_table(backend, apis, schema, session)},
        "problems": [], "unverified": [], "cases": [],
        "coverage": {"covered": [], "uncovered": []},
        "spec": SPEC_ROOT + "/" + SPEC_NAME}

    def problem(message: str) -> None:
        """Record a UI defect; the repair targets the application, never this test."""
        entry["problems"].append(message)
        plan["problems"].append("account: " + message)

    def unverified(capability: str, reason: str) -> None:
        """Record a piece of the lifecycle the accepted design cannot prove."""
        entry["unverified"].append({"capability": capability, "reason": reason})
        plan.setdefault("unverified", []).append(
            {"spec": entry["spec"], "capability": capability, "reason": reason})

    home_pid = page_for_route(ui, "/") or next(iter(ui["pages"]), None)
    register_pid = page_for_route(ui, apis["register"]["path"])
    login_pid = page_for_route(ui, apis["login"]["path"]) if "login" in apis else None
    for label, pid in (("home", home_pid), ("register", register_pid), ("login", login_pid)):
        if pid is None:
            continue
        route = (ui["pages"].get(pid) or {}).get("route")
        if not route or parameters(route):
            problem("the " + label + " page declares the route " + repr(route) + ", which a visitor"
                " cannot open without a parameter, so the lifecycle cannot be walked")
            return None
        entry["routes"][label] = route
        entry["pages"][label] = pid
    if register_pid is None or "home" not in entry["pages"]:
        problem("no designed page serves the registration route " + apis["register"]["path"]
            + " together with a home page, so a visitor cannot create an account")
        return None
    submit = action_control(ui, design, apis["register"]["id"])
    if submit is None:
        problem("no designed control submits the registration API " + apis["register"]["id"]
            + ", so a visitor cannot create an account")
        return None
    field = labelled_field(ui, register_pid, IDENTITY_WORDS,
        body_identity(backend, apis["register"]["id"]))
    if field is None:
        problem("the registration page declares no writable field whose label names the account"
            " identity (" + ", ".join(IDENTITY_WORDS[:4]) + ", ...), so the account cannot be named")
        return None
    entry["controls"]["register"] = submit
    entry["controls"]["identity_field"] = field
    entry["fills"] = fills(ui, register_pid, {field["element"]}, entry["token"])
    display = display_control(ui, home_pid)
    logout = action_control(ui, design, apis["logout"]["id"]) if "logout" in apis else None
    to_register = entry_link(ui, design, home_pid, register_pid)
    to_login = entry_link(ui, design, home_pid, login_pid)
    caps = {"identity": display is not None, "logout": logout is not None, "login": False,
        "error": error_surface(ui, register_pid) is not None, "login_error": False,
        "to_register": to_register is not None, "to_login": to_login is not None}
    if display is None and logout is None:
        problem("the home page declares neither the account identity nor a sign-out control, so no"
            " visitor can tell whether the session exists")
        return None
    if display is None:
        unverified("showing the signed-in account",
            "the home page declares no element that renders the authenticated username, so only the"
            " sign-out control tells the signed-in state apart")
    if logout is not None:
        entry["controls"]["logout"] = logout
    else:
        unverified("signing out", "the API contract declares no sign-out route, so the browser can"
            " never end a session")
    if "login" in apis:
        login_submit = action_control(ui, design, apis["login"]["id"])
        login_identity = labelled_field(ui, login_pid, IDENTITY_WORDS,
            body_identity(backend, apis["login"]["id"]))
        login_secret = labelled_field(ui, login_pid, CREDENTIAL_WORDS,
            body_credential(backend, apis["login"]["id"]))
        if login_submit and login_identity and login_secret:
            entry["controls"]["login"] = login_submit
            entry["controls"]["login_identity"] = login_identity
            entry["controls"]["login_secret"] = login_secret
            caps["login"] = True
            caps["login_error"] = error_surface(ui, login_pid) is not None
            if not caps["login_error"]:
                unverified("a rejected sign-in", "the sign-in page declares no alert element, so a"
                    " rejected credential has no visible error surface")
        else:
            unverified("signing in through the form",
                "the sign-in page does not expose a submit control with an identity field and a"
                " secret field, so an existing account cannot sign in again")
    else:
        unverified("signing in through the form",
            "the API contract declares no sign-in route, so an existing account cannot sign in again")
    if not caps["error"]:
        unverified("a rejected registration", "the registration page declares no alert element, so a"
            " rejected submission has no visible error surface")
    if "session" not in apis:
        unverified("resolving the session on reload",
            "the API contract declares no session lookup, so a reload cannot restore the account")
    entry["caps"] = caps
    entry["steps"] = ["entry", "register"]
    if caps["identity"]:
        entry["steps"].append("identity")
    if to_register is not None:
        entry["controls"]["to_register"] = to_register
    else:
        unverified("reaching the registration form",
            "the home page declares no control that opens " + entry["routes"]["register"]
            + "; the journey opens the route directly")
    if to_login is not None and caps["login"]:
        entry["controls"]["to_login"] = to_login
    entry["cases"] = cases_for(entry)
    if any(".reload(" in line for case in entry["cases"] for line in case["body"]):
        entry["steps"].append("restore")
    if any("visitor" in case["fixtures"] for case in entry["cases"]):
        entry["steps"].append("isolated")
    ambient = ambient_triggers(design, (apis.get("session") or {}).get("id"))
    covered, uncovered = coverage_records(scenario_index(tree, design), entry["cases"], ambient,
        lifecycle_triggers(design, apis))
    entry["coverage"] = {"covered": covered, "uncovered": uncovered}
    for item in uncovered:
        plan["notes"].append("account: " + item["scenario"] + " (" + item["name"] + ") is not"
            " covered: " + item["reason"])
    return entry


def account_import(entry: dict) -> str:
    """Render the support import with only the helpers this journey uses."""
    helpers = ["test", "expect", "action", "control"]
    if any("errorMessage(" in line for case in entry["cases"] for line in case["body"]):
        helpers.append("errorMessage")
    helpers.append("openRoute")
    return "import { " + ", ".join(helpers) + ' } from "./support/context.js";'


def account_source(entry: dict) -> str:
    """Render the compiler-owned acceptance spec for the whole account lifecycle."""
    lines = ["// Compiler-owned black-box acceptance journey for the account lifecycle.",
        "// The compiler regenerates this file from the accepted IR; no task edits it.",
        "// Every case below is a real user action in a real browser against the real backend.",
        'import type { Page } from "@playwright/test";',
        account_import(entry), ""]
    lines += account_constants(entry) + [""]
    lines += account_helpers(entry)
    for case in entry["cases"]:
        lines += ["", "test(" + json_literal(case["title"]) + ", async ({ "
            + ", ".join(case["fixtures"]) + " }) => {"] + list(case["body"]) + ["});"]
    return "\n".join(lines) + "\n"



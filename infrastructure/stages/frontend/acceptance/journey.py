"""Deterministic black-box CRUD journeys derived from the accepted IR and the UI design.

The compiler does not ask a model to write the acceptance journey. It derives one from
artifacts that were already accepted: ``backend_apis.json`` says which collection and item
routes exist, ``db_schema.json`` names the entity they persist, ``ui_design.json`` names
the pages, controls, labels and bindings, and ``frontend_triggers.json`` says which control
submits, navigates or deletes. Rendering is therefore reproducible and org-independent:
nothing here mentions a book, a note or any other concrete domain.

The derived journey is the external reviewer's journey, in this order:

1. open the entry page;
2. navigate to the object list through a real control;
3. navigate to the create page through a real control;
4. create the object with typed user input;
5. find it in the list by its own accessible name;
6. open its detail page by clicking that name;
7. reload and observe the stored record again;
8. observe the same record from a second, independent browser context;
9. modify it and observe the change;
10. delete it and observe that it is gone from both the list and the database.

A rejected submission is covered by a second case: the user stays on the form, the designed
error surface is visible, and neither the database nor a fresh browser context gained a row.

Whenever the derivation cannot find something the journey needs — a record identity control
with an exact accessible name, an editable field, a delete control — the plan records a
problem instead of inventing a weaker assertion. That is deliberate: the missing piece is a
UI defect, and the repair belongs to the UI design/implementation, never to the test.
"""

import json

from infrastructure.shared.parameters import PARAMETER, WHOLE_PARAMETER
from infrastructure.stages.backend.http_tests import normalize_name, slug
from infrastructure.stages.frontend.ui.design import (TEMPLATE_PLACEHOLDER, elements_of,
    page_elements)
from infrastructure.utils.files import save_text

SPEC_ROOT = "frontend/tests/acceptance"
SPEC_SUFFIX = ".journey.spec.ts"
TOKEN_PREFIX = "ARC-ACCEPT"
MAXIMUM_JOURNEYS = 2
ACTION_TYPES = {"button", "link"}
EDITABLE_TYPES = {"input", "select", "checkbox", "textarea"}
PASSWORD = "AcceptancePass1!"
EMAIL = "acceptance@example.com"
FUTURE_DATE = "2031-06-30"
PAST_DATE = "1990-03-15"
PHONE = "13800000000"
URL = "https://example.com"
def parameters(route: str) -> list[str]:
    """List the parameter names of an API path or a page route, in path order."""
    return [match.group(1) or match.group(2) for match in PARAMETER.finditer(route)]


def segments(route: str) -> list[str]:
    """Split a route into comparable segments; every parameter becomes the same token."""
    parts = (route or "").strip("/").split("/")
    return [":param" if WHOLE_PARAMETER.fullmatch(part) else part.lower() for part in parts]


def without_api(route: str) -> str:
    """Drop the generated backend's /api prefix so page routes compare with API paths."""
    trimmed = "/" + (route or "").strip("/")
    return trimmed[4:] or "/" if trimmed == "/api" or trimmed.startswith("/api/") else trimmed


def same_route(left: str, right: str) -> bool:
    """Compare a page route with an API path ignoring the /api prefix and parameter names."""
    return segments(without_api(left)) == segments(without_api(right))


def accessible_name(node: dict) -> str | None:
    """Return the static accessible name a designed element declares, if any.

    A prop that still holds a template placeholder is not a literal name, so no assertion is
    built from it: the design pass reports it as a ``placeholder`` problem instead, and the
    journey records the missing control rather than asserting text that cannot appear.
    """
    prop = node.get("prop") or {}
    for key in ("label", "text", "placeholder", "aria_label"):
        value = prop.get(key)
        if isinstance(value, str) and value.strip() and not TEMPLATE_PLACEHOLDER.search(value):
            return value
    return None


def writable(node: dict) -> bool:
    """Whether the host records user input for this element into a frontend variable."""
    return "user_write" in (node.get("mode") or [])


def templated_names(ui: dict) -> dict[str, str]:
    """Name each designed element whose name-bearing prop still holds a template placeholder.

    ``prop.text: "{book.title}"`` is not literal text: the rendered control carries the value, so
    an assertion built from the template can never match and the finding belongs to the UI design.
    Recording it as a derivation problem stops the layer before it asks the application to render
    a placeholder it can never render.
    """
    templated = {}
    for uid, node in sorted((ui.get("elements") or {}).items()):
        prop = node.get("prop") or {}
        for key in ("label", "text", "placeholder", "aria_label"):
            value = prop.get(key)
            if isinstance(value, str) and TEMPLATE_PLACEHOLDER.search(value):
                templated[uid] = f"{key}={value.strip()}"
                break
    return templated


def element_pages(ui: dict) -> dict[str, str]:
    """Map every designed element to the page whose subtree owns it."""
    owner = {}
    for pid in ui["pages"]:
        for uid in page_elements(ui, pid):
            owner.setdefault(uid, pid)
    return owner


def chrome_elements(ui: dict) -> set[str]:
    """Return the elements of shared chrome: a container a visitor sees on several pages.

    A header, a sidebar or a footer repeats on every page, so nothing inside it identifies one
    record of one collection: a search box there filters whatever page the visitor is on. Only
    the content of one page may name a record. An element the design explicitly marks as
    ``area: content`` is never treated as chrome, which lets a design put a record control in a
    region that repeats by accident.
    """
    pages_of: dict[str, set[str]] = {}
    for pid in ui["pages"]:
        for uid in page_elements(ui, pid):
            pages_of.setdefault(uid, set()).add(pid)
    chrome = set()
    for uid, pids in pages_of.items():
        node = ui["elements"].get(uid) or {}
        if len(pids) < 2 or (node.get("prop") or {}).get("area") == "content":
            continue
        chrome.add(uid)
    return chrome


def child_controls(ui: dict, uid: str, types: set[str]) -> list[tuple[str, dict]]:
    """Return the direct children of one element whose type is in ``types``, in design order."""
    return [(child_uid, child) for child_uid, child in ui["elements"].items()
        if child.get("parent") == uid and child.get("type") in types]


def trigger_control(ui: dict, source: str, types: set[str]) -> tuple[str, dict] | None:
    """Resolve the control that receives the click: a form delegates to its submit button."""
    node = ui["elements"].get(source)
    if node is None:
        return None
    if node.get("type") == "form":
        for child_uid, child in child_controls(ui, source, types):
            if accessible_name(child):
                return child_uid, child
        return None
    return (source, node) if node.get("type") in types else None


def api_actions(trigger: dict) -> set[str]:
    """Return the API IDs one designed trigger calls."""
    return {action.get("api") for action in trigger.get("action") or [] if action.get("kind") == "api"}


def ui_actions(trigger: dict) -> set[str]:
    """Return the UI element IDs one designed trigger updates or navigates to."""
    return {action.get("ui") for action in trigger.get("action") or [] if action.get("kind") == "ui"}


def api_index(backend: dict) -> dict:
    """Index the accepted API contract by ID with its normalized method and route."""
    indexed = {}
    for aid, api in (backend.get("apis") or {}).items():
        signature = api.get("signature") or {}
        method = str(signature.get("method") or "").upper()
        path = signature.get("path")
        if not method or not isinstance(path, str) or not path.startswith("/"):
            continue
        indexed[aid] = {"id": aid, "method": method, "path": path, "api": api}
    return indexed


def item_route(apis: dict, collection: str, methods: tuple[str, ...]) -> dict | None:
    """Find the single-segment item route of one collection addressing exactly one record."""
    prefix = collection.rstrip("/") + "/"
    candidates = [(aid, entry) for aid, entry in apis.items()
        if entry["method"] in methods and entry["path"].startswith(prefix)
        and WHOLE_PARAMETER.fullmatch(entry["path"][len(prefix):] or "")]
    return min(candidates, key=lambda pair: (len(pair[1]["path"]), pair[0]))[1] if candidates else None


def collection_route(apis: dict, collection: str, method: str) -> dict | None:
    """Find the collection route of one method, addressing the whole collection."""
    candidates = [(aid, entry) for aid, entry in apis.items()
        if entry["method"] == method and entry["path"] == collection]
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def resource_flows(backend: dict) -> list[dict]:
    """Derive create/read/update/delete flows; a flow needs a create route and a read-only item route."""
    apis = api_index(backend)
    flows = []
    for entry in sorted(apis.values(), key=lambda item: (item["path"], item["id"])):
        if entry["method"] != "POST" or parameters(entry["path"]):
            continue
        collection = entry["path"]
        detail = item_route(apis, collection, ("GET",))
        if detail is None:
            continue
        update, delete = item_route(apis, collection, ("PATCH", "PUT")), item_route(apis, collection, ("DELETE",))
        listed = collection_route(apis, collection, "GET")
        flows.append({"resource": collection.strip("/").split("/")[-1], "collection": collection,
            "item": detail["path"], "parameter": parameters(detail["path"])[-1],
            "create": entry["id"], "list": listed and listed["id"], "detail": detail["id"],
            "update": update and update["id"], "delete": delete and delete["id"]})
    # A flow that also updates and deletes exercises the whole journey, so it wins.
    return sorted(flows, key=lambda flow: (not (flow["update"] and flow["delete"]),
        not flow["list"], flow["collection"]))


def action_control(ui: dict, design: dict, aid: str) -> dict | None:
    """Find the designed control whose trigger submits one API."""
    for tid in sorted(design.get("triggers") or {}):
        trigger = design["triggers"][tid]
        if aid not in api_actions(trigger):
            continue
        control = trigger_control(ui, trigger.get("source"), ACTION_TYPES)
        if control and accessible_name(control[1]):
            return {"element": control[0], "node": control[1],
                "name": accessible_name(control[1]), "trigger": tid}
    return None


def navigation_control(ui: dict, design: dict, from_pid: str | None, to_pid: str | None) -> dict | None:
    """Find the designed control that takes a user from one page to another."""
    if not from_pid or not to_pid or from_pid == to_pid:
        return None
    root = (ui["pages"].get(to_pid) or {}).get("root")
    owner = element_pages(ui)
    for tid in sorted(design.get("triggers") or {}):
        trigger = design["triggers"][tid]
        if root not in ui_actions(trigger) or owner.get(trigger.get("source")) != from_pid:
            continue
        control = trigger_control(ui, trigger.get("source"), ACTION_TYPES)
        if control and accessible_name(control[1]):
            return {"element": control[0], "name": accessible_name(control[1]), "trigger": tid}
    route = (ui["pages"].get(to_pid) or {}).get("route") or "/"
    for uid, node in page_elements(ui, from_pid).items():
        href = (node.get("prop") or {}).get("href")
        if node.get("type") == "link" and href and same_route(href, route) and accessible_name(node):
            return {"element": uid, "name": accessible_name(node), "trigger": None}
    return None


def page_for_route(ui: dict, route: str) -> str | None:
    """Return the page whose route addresses the given API path or route."""
    for pid, page in ui["pages"].items():
        if same_route(page.get("route") or "/", route):
            return pid
    return None



def identity_control(ui: dict, design: dict, list_pid: str | None, detail_pid: str | None) -> dict | None:
    """Find the control a visitor clicks to open one record from the list.

    Controls inside shared chrome are never candidates: a navigation entry or a search result
    in the header belongs to every page, so it cannot be the identity of one record. What
    remains is the page content that names and opens the record.
    """
    if not list_pid or not detail_pid:
        return None
    page = ui["pages"][detail_pid]
    route, root, owner = page.get("route") or "/", page.get("root"), element_pages(ui)
    chrome = chrome_elements(ui)
    for tid in sorted(design.get("triggers") or {}):
        trigger = design["triggers"][tid]
        if root not in ui_actions(trigger) or owner.get(trigger.get("source")) != list_pid:
            continue
        control = trigger_control(ui, trigger.get("source"), ACTION_TYPES)
        if control and control[0] not in chrome:
            return {"element": control[0], "node": control[1], "name": accessible_name(control[1]),
                "trigger": tid}
    for uid, node in page_elements(ui, list_pid).items():
        href = (node.get("prop") or {}).get("href")
        if node.get("type") == "link" and href and same_route(href, route) and uid not in chrome:
            return {"element": uid, "node": node, "name": accessible_name(node), "trigger": None}
    return None


def identity_binding(control: dict) -> str | None:
    """Resolve the frontend variable whose value must be the record's accessible name.

    ``prop.name_from`` is the designed contract. Without it, exactly one bound variable is
    unambiguous enough to use; two or more variables mean the control renders its name as a
    concatenation (for example title + description + time), which no user or reviewer can
    identify a record by, so the caller records a design problem instead.
    """
    declared = (control["node"].get("prop") or {}).get("name_from")
    if isinstance(declared, str) and declared.strip():
        return declared
    bound = [var for var in control["node"].get("data") or [] if isinstance(var, str) and var]
    # Exactly one bound value is unambiguous. Two or more mean the control renders a
    # concatenation (a name plus a description, a status or a timestamp), which no user or
    # reviewer can identify a record by, so the design must declare prop.name_from.
    return bound[0] if len(bound) == 1 else None


def required_body(backend: dict, aid: str | None) -> list[str]:
    """List the request body members the contract marks as required, in contract order."""
    if not aid:
        return []
    body = (((backend.get("apis") or {}).get(aid) or {}).get("request") or {}).get("body") or {}
    required = body.get("required")
    return [str(name) for name in required] if isinstance(required, list) else []


def required_identity_property(backend: dict, aid: str | None) -> str | None:
    """Return the required body member that carries the new record's identity.

    The accepted contract decides which property names an object — ``name``, ``title``,
    ``subject``, ``label`` — so nothing here is written for one kind of product: the first
    required string member is that property, and a body that requires no string names no record
    the journey could find again.
    """
    declared = ((((backend.get("apis") or {}).get(aid) or {}).get("request") or {}).get("body")
        or {}).get("properties") or {}
    for name in required_body(backend, aid):
        kinds = (declared.get(name) or {}).get("type")
        types = kinds if isinstance(kinds, list) else [kinds]
        if "string" in [str(kind) for kind in types]:
            return name
    return None


def form_scope(ui: dict, design: dict, tid: str | None) -> set[str]:
    """Return the elements of the form one trigger submits from.

    A new record is identified by a field *this form submits*, so the form the create trigger
    belongs to is the only place its name may be typed. Everything else on the page — a header
    search box, a filter, a toolbar — is not part of that submission.
    """
    uid = ((design.get("triggers") or {}).get(tid or "") or {}).get("source")
    while isinstance(uid, str) and uid in (ui.get("elements") or {}):
        node = ui["elements"][uid]
        if node.get("type") == "form":
            return {uid, *elements_of(ui, uid)}
        uid = node.get("parent")
    return set()


def identity_field(ui: dict, design: dict, backend: dict, aid: str | None, tid: str | None,
        pid: str | None) -> tuple[dict | None, str | None]:
    """Choose the field of the create form that carries the new record's identity.

    The candidates are only the fields the create trigger really submits: those inside the form
    it submits from and, when the trigger declares the variables it reads, those bound to one of
    them. A page-wide search box can therefore never be mistaken for the record name — the
    mistake that made a save trigger read the header search variable and left the name empty.

    The property comes from the accepted contract, the field from the accepted design and the
    label is the accessible name the rendered journey asserts, so the same function derives the
    same field for books, tickets or notes. When nothing matches, the caller records a problem
    and renders no journey: guessing would produce a test that types an unrelated control and
    then blames the application for the empty name.
    """
    target = required_identity_property(backend, aid)
    if not pid:
        return None, target
    scope = form_scope(ui, design, tid)
    submitted = {var for var in ((design.get("triggers") or {}).get(tid or "") or {}).get("read") or []
        if isinstance(var, str)}
    wanted = normalize_name(target or "")
    ranked = []
    for index, (uid, node) in enumerate(page_elements(ui, pid).items()):
        if not writable(node) or node.get("type") not in {"input", "textarea"}:
            continue
        if not accessible_name(node) or (scope and uid not in scope):
            continue
        bound = [var for var in node.get("data") or [] if isinstance(var, str) and var]
        label = normalize_name(accessible_name(node) or "")
        tails = [normalize_name(var.rsplit(".", 1)[-1]) for var in bound]
        rank = 0 if wanted and label == wanted else 1 if wanted and wanted in tails else 2
        ranked.append((rank, index, uid, node, bound))
    if not ranked:
        return None, target
    chosen = [item for item in ranked if submitted and set(item[4]) & submitted] or ranked
    _, _, uid, node, _ = min(chosen, key=lambda item: (item[0], item[1]))
    return {"element": uid, "node": node, "name": accessible_name(node)}, target



def error_surface(ui: dict, pid: str | None) -> dict | None:
    """Return the designed alert a rejection must appear in."""
    if not pid:
        return None
    for uid, node in page_elements(ui, pid).items():
        if node.get("type") == "alert":
            return {"element": uid, "node": node, "name": accessible_name(node)}
    return None


def body_properties(backend: dict, aid: str | None) -> list[str]:
    """List the request body members an API declares."""
    if not aid:
        return []
    body = ((backend.get("apis") or {}).get(aid) or {}).get("request") or {}
    return list(((body.get("body") or {}).get("properties") or {}))


def value_for(node: dict, name: str | None, token: str, index: int) -> tuple[str, str] | None:
    """Choose a plausible value for one designed control from its type and its label."""
    prop = node.get("prop") or {}
    kind = node.get("type")
    input_type = str(prop.get("input_type") or "text").lower()
    if kind == "checkbox":
        return "check", "true"
    if kind == "select":
        options = [str(option) for option in prop.get("options") or [] if str(option).strip()]
        return ("select", options[0]) if options else None
    if input_type == "radio":
        return "check", "true"
    lowered = (name or "").lower()
    if input_type == "password" or "password" in lowered:
        return "fill", PASSWORD
    if input_type == "email" or "email" in lowered:
        return "fill", EMAIL
    if input_type == "date" or "date" in lowered:
        future = any(hint in lowered for hint in ("expir", "end", "due", "future", "return"))
        return "fill", FUTURE_DATE if future else PAST_DATE
    if input_type == "tel" or "phone" in lowered or "mobile" in lowered:
        return "fill", PHONE
    if input_type in {"number", "range"}:
        return "fill", "1"
    if input_type == "url" or "url" in lowered or "website" in lowered:
        return "fill", URL
    if input_type == "time":
        return "fill", "09:00"
    if kind in {"input", "textarea"} and input_type in {"", "text", "search"}:
        return "fill", f"{token}-{index}"
    return None


def fills(ui: dict, pid: str | None, exclude, token: str, scope=frozenset()) -> list[dict]:
    """Describe every designed field a user must fill on a page, in design order.

    When ``scope`` names the form a trigger submits, only that form's fields are filled: typing
    into a search box or a filter the form never submits would fake user input the application
    does not read.
    """
    if not pid:
        return []
    result, seen = [], set()
    for uid, node in page_elements(ui, pid).items():
        if uid in exclude or not writable(node) or node.get("type") not in EDITABLE_TYPES:
            continue
        if scope and uid not in scope:
            continue
        binding = tuple(node.get("data") or [uid])
        if binding in seen or not accessible_name(node):
            continue
        seen.add(binding)
        chosen = value_for(node, accessible_name(node), token, len(result) + 1)
        if chosen:
            result.append({"element": uid, "name": accessible_name(node),
                "kind": chosen[0], "value": chosen[1]})
    return result



def edit_field_for(ui: dict, pid: str | None, properties: list[str], exclude=()) -> dict | None:
    """Choose the field a visitor modifies: a text-like field the update API accepts first."""
    if not pid:
        return None
    wanted = [normalize_name(name) for name in properties]
    candidates = []
    for uid, node in page_elements(ui, pid).items():
        if uid in exclude or not writable(node) or node.get("type") not in EDITABLE_TYPES:
            continue
        if not accessible_name(node) or node.get("type") == "checkbox":
            continue
        binding = (node.get("data") or [""])[0]
        tail = normalize_name(str(binding).rsplit(".", 1)[-1])
        matches = any(tail == name or (name and name in tail) for name in wanted)
        text_like = node.get("type") in {"input", "textarea"} and str(
            (node.get("prop") or {}).get("input_type") or "text").lower() in {"", "text", "search"}
        candidates.append((0 if text_like and matches else 1 if text_like else 2, uid, node))
    if not candidates:
        return None
    _, uid, node = min(candidates, key=lambda item: (item[0], item[1]))
    return {"element": uid, "node": node, "name": accessible_name(node)}



def journey_entry(ui: dict, design: dict, backend: dict, flow: dict, plan: dict) -> dict | None:
    """Derive one executable journey for one CRUD flow, or record why it cannot exist."""
    resource = slug(flow["resource"])
    token = f"{TOKEN_PREFIX}-{resource.upper()}"
    entry = {"resource": flow["resource"], "slug": resource, "collection": flow["collection"],
        "item": flow["item"], "parameter": flow["parameter"],
        "apis": {key: flow[key] for key in ("create", "list", "detail", "update", "delete")},
        "token": token, "edited": token + "-EDITED", "problems": [], "derivation_problems": [],
        "steps": [], "pages": {}, "routes": {}, "controls": {}, "fills": [], "database": {},
        "spec": f"{SPEC_ROOT}/{resource}{SPEC_SUFFIX}"}

    def derivation(message: str) -> None:
        """Record a defect of the derivation itself, which application repair cannot address."""
        entry["derivation_problems"].append(message)
        plan.setdefault("derivation_problems", []).append(flow["collection"] + ": " + message)
        problem(message)

    def problem(message: str) -> None:
        """Record a UI defect; the repair targets the application, never this test."""
        entry["problems"].append(message)
        plan["problems"].append(flow["collection"] + ": " + message)

    create = action_control(ui, design, flow["create"])
    if templated := templated_names(ui):
        derivation("the UI design still holds template placeholders ("
            + ", ".join(uid + " " + value for uid, value in templated.items())
            + "), so no control can be located by its designed name; the design must write the"
            " literal wording or prop.name_from")
        return None
    if create is None:
        problem("no designed control submits the create API " + flow["create"]
            + ", so a visitor cannot create this object")
        return None
    owner = element_pages(ui)
    create_pid = owner.get(create["element"])
    detail_pid = page_for_route(ui, flow["item"])
    if detail_pid is None:
        problem("no page serves the item route " + flow["item"] + ", so the record's detail"
            " page is missing and the record cannot be opened after it is created")
    list_pid = page_for_route(ui, flow["collection"])
    identity = None
    for pid in ([list_pid] if list_pid else []) + [pid for pid in ui["pages"] if pid != list_pid]:
        identity = identity_control(ui, design, pid, detail_pid) if detail_pid else None
        if identity is not None:
            list_pid = list_pid or pid
            break
    if list_pid is None:
        problem("no page lists " + flow["collection"] + ", so a created object cannot be found again")
        return None
    home_pid = page_for_route(ui, "/") or next(iter(ui["pages"]))
    for label, pid in (("home", home_pid), ("list", list_pid), ("create", create_pid)):
        route = (ui["pages"].get(pid) or {}).get("route")
        if not route or parameters(route):
            plan["notes"].append(flow["collection"] + ": the " + label + " page route " + repr(route)
                + " needs path parameters; this journey is derived for top-level resources only")
            return None
        entry["routes"][label] = route
    entry["pages"] = {"home": home_pid, "list": list_pid, "create": create_pid, "detail": detail_pid}
    entry["routes"]["detail"] = (ui["pages"].get(detail_pid) or {}).get("route") or ""
    entry["controls"]["create"] = {"element": create["element"], "name": create["name"]}
    entry["steps"] += ["open", "list", "create"]

    to_list = navigation_control(ui, design, home_pid, list_pid)
    entry["controls"]["to_list"] = to_list and {"element": to_list["element"], "name": to_list["name"]}
    if to_list is None and list_pid != home_pid:
        plan["notes"].append(flow["collection"] + ": no control on the entry page opens the list page; "
            "the journey opens " + entry["routes"]["list"] + " directly")
    to_create = navigation_control(ui, design, list_pid, create_pid)
    entry["controls"]["to_create"] = to_create and {"element": to_create["element"], "name": to_create["name"]}
    if to_create is None and create_pid != list_pid:
        plan["notes"].append(flow["collection"] + ": no control on the list page opens the create page; "
            "the journey opens " + entry["routes"]["create"] + " directly")

    binding = identity_binding(identity) if identity else None
    if identity is None:
        problem("no control on the list page opens one record's detail page, so a visitor cannot open the object")
    elif binding is None:
        problem("the record control " + identity["element"] + " does not expose one record name; add a"
            " dedicated identity control (prop.name_from) whose accessible name is exactly the record name"
            " instead of a concatenation of name, description and timestamp")
    else:
        entry["controls"]["identity"] = {"element": identity["element"],
            "name_from": binding, "name": identity["name"]}

    field, target = identity_field(ui, design, backend, flow["create"],
        (create.get("trigger") or None), create_pid)
    if field is None:
        derivation("the create form on page " + str(create_pid) + " submits no field for the required"
            " property " + repr(target) + " of " + flow["create"] + ", so the new record would have"
            " no name to type; the derivation refuses to type the name into an unrelated control")
        return None
    entry["controls"]["identity_field"] = {"element": field["element"], "name": field["name"],
        "property": target}
    entry["fills"] = fills(ui, create_pid, {field["element"]}, token,
        form_scope(ui, design, create.get("trigger") or None))
    if binding:
        entry["steps"].append("find")
        if entry["routes"]["detail"]:
            entry["steps"] += ["open-detail", "reload", "independent"]

    update = action_control(ui, design, flow["update"]) if flow["update"] else None
    if flow["update"] and update is None:
        problem("the update API " + str(flow["update"]) + " has no designed control, so a visitor cannot"
            " modify the object")
    if update is not None and owner.get(update["element"]) == detail_pid:
        edit = edit_field_for(ui, detail_pid, body_properties(backend, flow["update"]),
            exclude={field["element"]})
        if edit is None:
            problem("the detail page has no editable field, so the journey cannot modify the object")
        else:
            chosen = value_for(edit["node"], edit["name"], entry["edited"], 1)
            entry["controls"]["update"] = {"element": update["element"], "name": update["name"]}
            entry["controls"]["edit_field"] = {"element": edit["element"], "name": edit["name"],
                "kind": (chosen or ("fill", entry["edited"]))[0],
                "value": (chosen or ("fill", entry["edited"]))[1]}
            entry["steps"].append("update")
    elif update is not None:
        plan["notes"].append(flow["collection"] + ": the update control is not on the detail page, so the"
            " journey cannot reach it without an extra navigation step")

    delete = action_control(ui, design, flow["delete"]) if flow["delete"] else None
    if flow["delete"] and delete is None:
        problem("the delete API " + str(flow["delete"]) + " has no designed control, so a visitor cannot"
            " delete the object")
    if delete is not None and owner.get(delete["element"]) in {detail_pid, list_pid}:
        entry["controls"]["delete"] = {"element": delete["element"], "name": delete["name"],
            "page": owner.get(delete["element"])}
        entry["steps"] += ["delete", "gone"]

    surface = error_surface(ui, create_pid)
    if surface is None:
        problem("the create page declares no alert element, so a rejected submission has no visible"
            " error surface")
    else:
        entry["controls"]["error"] = {"element": surface["element"], "name": surface["name"]}
    if "find" not in entry["steps"]:
        # Without a control that identifies the record, the journey cannot locate it again, so
        # update and delete cannot be verified. The missing identity is already reported above.
        entry["steps"] = [step for step in entry["steps"] if step not in {"update", "delete", "gone"}]
        for key in ("update", "edit_field", "delete"):
            entry["controls"].pop(key, None)
    entry["database"] = {"table": entity_table(backend, flow["create"], plan.get("schema") or {}),
        "checks": ["stores", "total"]}
    return entry


def entity_table(backend: dict, aid: str, schema: dict) -> str | None:
    """Map an API's write references onto a schema table; recorded for the report only."""
    tables = list((schema.get("entities") or {}))
    references = [str(reference).split(".")[0] for reference in
        ((backend.get("apis") or {}).get(aid) or {}).get("write") or []]
    for reference in references:
        wanted = normalize_name(reference)
        for table in tables:
            if normalize_name(table) == wanted or normalize_name(table).endswith(wanted):
                return table
    return None



def journey_plan(tree, schema, backend: dict, ui: dict, design: dict) -> dict:
    """Derive the acceptance journeys for one generated application.

    The plan is the acceptance layer's contract with the repair loop: ``journeys`` are the
    specs that will be rendered and run, ``problems`` are UI defects the derivation exposed
    (a missing identity control, a missing delete control, a form without an error surface),
    ``unverified`` are the capabilities the accepted IR cannot prove, and ``notes`` explain
    what was deliberately left out and why.

    Two families of journeys are derived: the CRUD flows of the API contract, and the account
    lifecycle (register, session, sign out, sign in again, rejected credentials). An application
    that only manages accounts declares no CRUD pair, so without the second family its
    acceptance layer would report ``NOT_APPLICABLE`` and accept an application no test drove.
    """
    plan = {"journeys": [], "problems": [], "derivation_problems": [], "notes": [],
        "unverified": [], "schema": schema or {}}
    if not (ui.get("pages") or {}):
        plan["notes"].append("The UI design declares no page, so no visitor journey can be accepted.")
        return plan
    flows = resource_flows(backend)
    if not flows:
        plan["notes"].append("The API contract declares no create plus read pair, so no CRUD journey"
            " can be derived for this application.")
    for flow in flows:
        if len(plan["journeys"]) >= MAXIMUM_JOURNEYS:
            plan["notes"].append(flow["collection"] + ": the journey budget of "
                + str(MAXIMUM_JOURNEYS) + " is already used")
            continue
        entry = journey_entry(ui, design, backend, flow, plan)
        if entry is not None:
            plan["journeys"].append(entry)
    # Imported here, not at module scope: the account module builds on this module's primitives,
    # so a module-level import would be circular.
    from infrastructure.stages.frontend.acceptance.account import account_journey
    account = account_journey(tree, schema, backend, ui, design, plan)
    if account is not None:
        plan["journeys"].append(account)
    if not plan["journeys"]:
        plan["notes"].append("No derived flow could be rendered as a journey; see the problems above.")
    return plan


def path_expression(route: str) -> str:
    """Render a TypeScript expression building one URL from the record name."""
    parts, position = [], 0
    for match in PARAMETER.finditer(route):
        literal = route[position:match.start()]
        if literal:
            parts.append(json_literal(literal))
        parts.append("encodeURIComponent(name)")
        position = match.end()
    if route[position:]:
        parts.append(json_literal(route[position:]))
    return " + ".join(parts) or '""'


def json_literal(value) -> str:
    """Render a JavaScript string literal without depending on Python's repr."""
    return json.dumps(value, ensure_ascii=False)


def statement(item: dict) -> str:
    """Render one user input statement for a designed control."""
    target = "control(page, " + json_literal(item["name"]) + ")"
    if item["kind"] == "select":
        return "await " + target + ".selectOption(" + json_literal(item["value"]) + ");"
    if item["kind"] == "check":
        return "await " + target + ".check();"
    return "await " + target + ".fill(" + json_literal(item["value"]) + ");"



def import_line(entry: dict) -> str:
    """Render the support import with only the helpers this journey uses."""
    steps, controls = entry["steps"], entry["controls"]
    helpers = ["test", "expect"]
    if any(controls.get(key) for key in ("to_list", "to_create", "create", "update", "delete")):
        helpers.append("action")
    helpers.append("control")
    if "find" in steps:
        helpers.append("record")
    helpers += ["errorMessage", "openRoute"]
    if "independent" in steps:
        helpers.append("expectIndependentlyVisible")
    helpers.append("expectIndependentlyMissing")
    return "import { " + ", ".join(helpers) + ' } from "./support/context.js";'


def open_steps(entry: dict) -> list[str]:
    """Render steps 1-3: open the application, the object list and the create page."""
    controls, pages = entry["controls"], entry["pages"]
    lines = ["  // 1. Open the application.", "  await openRoute(page, HOME);",
        "  await expect(page).toHaveURL(HOME);"]
    to_list = controls.get("to_list")
    if to_list:
        name = json_literal(to_list["name"])
        lines += ["  // 2. Reach the object list through a real navigation control.",
            "  await expect(action(page, " + name + ")).toHaveAccessibleName(" + name + ");",
            "  await action(page, " + name + ").click();",
            "  await expect(page).toHaveURL(LIST);"]
    else:
        lines += ["  // 2. Open the object list.", "  await openRoute(page, LIST);",
            "  await expect(page).toHaveURL(LIST);"]
    to_create = controls.get("to_create")
    if to_create:
        name = json_literal(to_create["name"])
        lines += ["  // 3. Reach the create page through a real navigation control.",
            "  await expect(action(page, " + name + ")).toHaveAccessibleName(" + name + ");",
            "  await action(page, " + name + ").click();",
            "  await expect(page).toHaveURL(CREATE);"]
    elif pages["create"] != pages["list"]:
        lines += ["  // 3. Open the create page.", "  await openRoute(page, CREATE);",
            "  await expect(page).toHaveURL(CREATE);"]
    return lines


def create_steps(entry: dict) -> list[str]:
    """Render step 4: type the object into the designed form and submit it."""
    field = entry["controls"]["identity_field"]["name"]
    create = entry["controls"]["create"]["name"]
    lines = ["  // 4. Create the object with real user input.",
        "  await control(page, " + json_literal(field) + ").fill(RECORD);"]
    lines += ["  " + statement(fill) for fill in entry["fills"]]
    lines += ["  await expect(action(page, " + json_literal(create) + ")).toHaveAccessibleName("
        + json_literal(create) + ");",
        "  await action(page, " + json_literal(create) + ").click();"]
    return lines


def read_steps(entry: dict) -> list[str]:
    """Render steps 5-8: find the object by name, open it, reload, and observe it in a second context."""
    lines = ["  // 5. The object appears in the list under its own accessible name.",
        "  await openRoute(page, LIST);",
        "  await expect(record(page, RECORD).first(), IDENTITY_HINT).toBeVisible();",
        "  expect(database.stores(RECORD)).toBeGreaterThan(0);",
        "  // 6. Open the object by clicking its name.",
        "  await record(page, RECORD).first().click();",
        "  await expect(page).toHaveURL(recordPath(RECORD));",
        "  // 7. A reload loads the object again from stored data, not from client memory.",
        "  await page.reload();",
        "  await expect(page).toHaveURL(recordPath(RECORD));",
        "  await expect(page.getByText(RECORD).first()).toBeVisible();",
        "  expect(database.stores(RECORD)).toBeGreaterThan(0);"]
    if "independent" in entry["steps"]:
        lines += ["  // 8. A second, independent browser context sees the same stored object.",
            "  await expectIndependentlyVisible(visitor, recordPath(RECORD), RECORD, test.info());"]
    return lines


def update_steps(entry: dict) -> list[str]:
    """Render step 9: modify the object and read the stored value back after a fresh load."""
    edit = entry["controls"]["edit_field"]
    update = json_literal(entry["controls"]["update"]["name"])
    change = ("await control(page, " + json_literal(edit["name"]) + ").fill(EDITED);"
        if edit.get("kind", "fill") == "fill"
        else statement({"name": edit["name"], "kind": edit["kind"], "value": edit["value"]}))
    return ["  // 9. Modify the object and load the stored value back.",
        "  await openRoute(page, recordPath(RECORD));",
        "  " + change,
        "  await expect(action(page, " + update + ")).toHaveAccessibleName(" + update + ");",
        "  await action(page, " + update + ").click();",
        "  await openRoute(page, recordPath(RECORD));",
        "  await expect(control(page, " + json_literal(edit["name"]) + ")).toHaveValue(EDITED);",
        "  expect(database.stores(EDITED)).toBeGreaterThan(0);"]


def delete_steps(entry: dict) -> list[str]:
    """Render step 10: delete the object and prove the side effect everywhere."""
    delete = entry["controls"]["delete"]
    target = json_literal(delete["name"])
    destination = "recordPath(RECORD)" if delete.get("page") == entry["pages"]["detail"] else "LIST"
    return ["  // 10. Delete the object and prove the side effect.",
        "  await openRoute(page, " + destination + ");",
        "  await expect(action(page, " + target + ")).toHaveAccessibleName(" + target + ");",
        "  await action(page, " + target + ").click();",
        "  await openRoute(page, LIST);",
        "  await expect(record(page, RECORD)).toHaveCount(0);",
        "  expect(database.stores(RECORD)).toBe(0);",
        "  await expectIndependentlyMissing(visitor, LIST, RECORD, test.info());"]


def journey_source(entry: dict) -> str:
    """Render one compiler-owned acceptance spec for one derived journey."""
    steps = entry["steps"]
    controls = entry["controls"]
    detail = entry["routes"].get("detail") or ""
    dynamic = bool(detail and parameters(detail))
    lines = [
        "// Compiler-owned black-box acceptance journey for the " + entry["resource"] + " resource.",
        "// The compiler regenerates this file from the accepted IR; no task edits it.",
        "// Every step below is a real user action in a real browser against the real backend.",
        import_line(entry), "",
        "const RECORD = " + json_literal(entry["token"]) + ";",
        "const EDITED = " + json_literal(entry["edited"]) + ";",
        "const HOME = " + json_literal(entry["routes"]["home"]) + ";",
        "const LIST = " + json_literal(entry["routes"]["list"]) + ";",
        "const CREATE = " + json_literal(entry["routes"]["create"]) + ";",
    ]
    if "find" in steps:
        lines += ['const IDENTITY_HINT = "no control in the list exposes the record name as its exact"',
            '  + " accessible name; a record control must render only the record name";']
    if detail:
        argument = "name" if dynamic else "_name"
        lines += ["", "// The URL of one record: a dynamic route encodes the name like any user input."
            if dynamic else "// The detail route of this resource takes no parameter.",
            "function recordPath(" + argument + ": string): string {",
            "  return " + path_expression(detail) + ";", "}"]
    lines += ["", 'test("a visitor completes the ' + entry["resource"]
        + ' journey through the UI", async ({ page, database, visitor }) => {']
    lines += open_steps(entry) + create_steps(entry)
    if "find" in steps:
        lines += read_steps(entry)
    if "update" in steps:
        lines += update_steps(entry)
    if "delete" in steps:
        lines += delete_steps(entry)
    lines += ["});", "", 'test("a rejected submission shows an error and writes nothing",'
        ' async ({ page, database, visitor }) => {',
        "  await openRoute(page, CREATE);",
        "  await expect(page).toHaveURL(CREATE);",
        "  const stored = database.total();"]
    lines += ["  " + statement(fill) for fill in entry["fills"]]
    lines += ["  // Leave the identifying field empty: the application must reject this submission.",
        "  await control(page, " + json_literal(controls["identity_field"]["name"]) + ').fill("");',
        "  await action(page, " + json_literal(controls["create"]["name"]) + ").click();",
        "  // The user stays on the form: a rejected request must never look like success.",
        "  await expect(page).toHaveURL(CREATE);"]
    if controls.get("error"):
        lines += ["  // The designed error surface reports the rejection.",
            "  await expect(errorMessage(page).first()).toBeVisible();"]
    lines += ["  // Nothing was written, and no other visitor can observe a row.",
        "  expect(database.total()).toBe(stored);",
        "  expect(database.stores(RECORD)).toBe(0);",
        "  await expectIndependentlyMissing(visitor, LIST, RECORD, test.info());",
        "});"]
    return "\n".join(lines) + "\n"


def journey_renderer(entry: dict):
    """Pick the renderer of one derived journey; the account family renders its own spec."""
    if entry.get("kind") == "account":
        # Imported here for the same reason journey_plan imports it lazily.
        from infrastructure.stages.frontend.acceptance.account import account_source
        return account_source(entry)
    return journey_source(entry)


def write_journeys(output, plan: dict) -> list[str]:
    """Render every derived journey and remove the specs of journeys that no longer exist."""
    written = []
    for entry in plan["journeys"]:
        save_text(output / entry["spec"], journey_renderer(entry))
        written.append(entry["spec"])
    directory = output / SPEC_ROOT
    if directory.is_dir():
        for path in sorted(directory.glob("*" + SPEC_SUFFIX)):
            relative = path.relative_to(output).as_posix()
            if relative not in written:
                path.unlink()
    return written

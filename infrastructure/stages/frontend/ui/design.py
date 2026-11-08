"""Canonical UI/data records and incremental design updates."""

from copy import deepcopy
from re import compile as compile_pattern

#: A template placeholder is not a literal name: ``text: "{book.title}"`` can never match the
#: rendered control, so a name-bearing prop that still holds one is a design defect. Data-derived
#: names belong in ``prop.name_from``; static wording must be written out.
TEMPLATE_PLACEHOLDER = compile_pattern(r"[{][^{}]*[}]")



INSTRUCTIONS = """Design a complete UI and its frontend data for the current requirement.
The result is consumed by a component generator: page metadata alone cannot produce
any controls. Design the actual element tree and variables, not just pages or fixtures.
Use requirement scenarios and visual references as evidence; reuse retrieved UI records
where appropriate. This is design only: return no code, tests, triggers or API definitions.

OUTPUT CONTRACT
Return exactly one complete JSON object, with these FIVE SIBLING root keys in this order:
1. bindings: frontend variable definitions.
2. elements: the actual page roots, containers, controls and displayed content.
3. pages: route, root element, image references and display fixture metadata.
4. requirement_ui: element IDs used by the current requirement.
5. step_ui: current scenario step IDs mapped to element IDs.
Write every section in the final JSON content. Mentioning a section in reasoning does
not return it. Do not stop after a page record. Do not nest elements or bindings inside
pages. For a new page, write its root and controls in elements and all variables they
use in bindings. Fixture variables do not create bindings or elements automatically.

RECORD DEFINITIONS
bindings maps VARIABLE NAMES to {dtype, constraints, initial, source}. dtype is a JSON
type name: string, integer, number, boolean, array, object or null. constraints is an
array of descriptions, initial is the application default, and source describes the
origin. Include input, selection, result, loading or error data needed by this UI.

elements maps ELEMENT IDs to {type, parent, mode, data, prop}. A page root has type
"page" and parent=null. Other elements have a parent ELEMENT ID. Use ordinary UI types:
container, form, input, checkbox, select, button, text, heading, link, list, item, image,
alert or table. mode is an array containing user_read and/or user_write, or [].
data is an array of VARIABLE NAMES, not variable objects. prop contains static text,
label, placeholder, input_type, options, style, href, alt or heading level. Optional
prop.visible is a boolean or {var:VARIABLE_NAME, equals:VALUE}. Express business
behavior through triggers later, not expression strings embedded in props.
An image element copies a supplied reference image only when that image is page content: set
prop.image to the supplied image ID and the compiler serves it at the URL given in the
requirement evidence. A layout reference that you reproduce with real elements needs no image
element, and a page never renders a whole reference screenshot in place of its controls.

REQUIRED CONTROLS
The request's required_controls lists the links, buttons and checkboxes that the wording of this
requirement or of a parent requirement demands, each with its role, exact accessible name and
href. Every listed control must exist on the pages this requirement uses, with that role, that
exact name and, for a link, that exact href. A control marked inherited=true comes from a parent
requirement and is never restated here: repeat it as shared header, navigation or footer content
on this requirement's pages, because a visitor sees the parent's controls on every page beneath
it. Do not rename a control, translate it, split its name across elements, wrap it in another
control, or replace it with text, an icon alone or a link to another route. A control that is
absent from the design cannot be recovered by a later stage.


Design for executable user journeys, not just screenshots: every required WHEN must
have a reachable page, actual control with stable element ID, writable binding if
it accepts input, and a visible location for the THEN result or error. Include
navigation controls and appropriate signed-in/signed-out display states only when
required. Keep form field labels/types/options aligned with exact requirement text.
Avoid duplicate pages, speculative controls, and display-only substitutes for actions.

ACCESSIBLE NAMES are how a user, a screen reader and an external black-box reviewer find and
operate a control, so design them explicitly:
- Every interactive element declares its accessible name: prop.label for input, select and
  checkbox (the visible field label), prop.text for a button or link, or prop.name_from when
  the name is a data value rather than literal text.
- prop.name_from is a VARIABLE NAME whose value must BE that control's accessible name,
  exactly and alone. Use it on the control that opens one record from a list, so every record
  can be identified by its own name. Never design a record control that renders the name
  together with a description, timestamp or another value: put those values on separate
  elements beside it.
- Keep one label per control and keep labels unique within one form; a duplicated or missing
  label makes a field unaddressable.
- Every form that can be rejected designs an alert element for the rejection, so the failure
  is visible where the user is and never looks like success.

pages maps PAGE IDs to {name, route, root, images, fixtures}. route is a URL path
beginning with /, root is an ELEMENT ID, images lists supplied image IDs. Write a dynamic
route segment as :name or {name}, for example /books/:bookName when the requirement opens a
specific book; the running application matches such URLs, decodes their parameters and
passes them to the page and its triggers. fixtures maps display-state names
to variable overrides; include default={} plus representative states within max_fixtures.
Bindings define defaults; fixtures only override them for display tests. Fixture state
must not become an invented production API response. Use realistic default state;
fixture-only success/error values must not leak into runtime defaults.
Preserve explicit image.page hints.

requirement_ui is an array of ELEMENT IDs. step_ui maps this requirement's actual step
IDs to arrays of ELEMENT IDs; use [] for a step without UI.

UPDATES AND REUSE
Return complete records for new or changed items. Each supplied record replaces its
previous definition, so preserve unrelated content on shared pages. Unchanged records
can be omitted FROM their maps, but never omit any of the five root keys. Use {} for
an unchanged map, and list reused element IDs in requirement_ui/step_ui. all_pages is
only a page catalogue; retrieved_pages supplies reusable element and variable records.
A requirement without UI returns all five sections with empty maps/lists.

NATIVE INPUT AND SCOPE
The host synchronizes native input/change values for user_write bindings. UI has no
DB obligations or DB field-mapping model. Preserve visible labels in their original
language. Images describe layout and style, not hidden business logic.

The complete example below demonstrates the final response, not an intermediate page
summary. Replace its IDs, labels and state with the current requirement's evidence.
"""


OUTPUT_TEMPLATE = {"bindings": {}, "elements": {}, "pages": {}, "requirement_ui": [], "step_ui": {}}


class UIResponseError(ValueError):
    """Attach the concrete output layout after a model record cannot be consumed."""
    def __init__(self, error, proposal):
        super().__init__(f"Cannot read UI/data response: {error}")
        self.feedback = {"response_template": OUTPUT_TEMPLATE,
            "missing_sections": [key for key in OUTPUT_TEMPLATE if not isinstance(proposal, dict) or key not in proposal],
            "repair": "Write the missing variable and element records in the FINAL JSON, not just in reasoning. Return bindings and elements FIRST, then pages, requirement_ui and step_ui as five sibling root keys. A new page needs its root and controls, their variable definitions and actual scenario associations. Do not repeat the rejected pages-only answer or merely add empty maps for a new page."}


def empty_design() -> dict:
    """Keep UI/data and requirement associations in five independent registries."""
    return {"pages": {}, "elements": {}, "bindings": {}, "requirements": {}, "steps": {}}


def page_elements(design: dict, pid: str) -> dict:
    """Return the IDs in one page's UI subtree, including its root."""
    root = design["pages"][pid]["root"]
    selected = {root}
    while True:
        expanded = selected | {uid for uid, node in design["elements"].items() if node.get("parent") in selected}
        if expanded == selected:
            return {uid: deepcopy(node) for uid, node in design["elements"].items() if uid in selected}
        selected = expanded


def page_context(design: dict, pid: str) -> dict:
    """Return one canonical page together with its UI subtree and the data bindings used by that subtree."""
    elements = page_elements(design, pid)
    variables = {var for node in elements.values() for var in node["data"]}
    variables.update(node.get("prop", {})["visible"]["var"] for node in elements.values() if isinstance(node.get("prop", {}).get("visible"), dict))
    return {"id": pid, **deepcopy(design["pages"][pid]), "elements": elements,
        "bindings": {var: deepcopy(design["bindings"][var]) for var in sorted(variables) if var in design["bindings"]}}


def accessibility_problems(design: dict) -> list[dict]:
    """Report design records that leave a control unaddressable by name.

    An external acceptance reviewer locates controls by role and accessible name, so a design
    that renders no name, or a name assembled from several values, cannot be verified without
    guessing. The findings are diagnostics for the UI design/implementation, never a reason to
    weaken a test.
    """
    problems = []
    bindings = set(design.get("bindings") or {})
    for uid, node in sorted((design.get("elements") or {}).items()):
        prop = node.get("prop") or {}
        templated = next((key for key in ("label", "text", "placeholder", "aria_label")
            if isinstance(prop.get(key), str) and TEMPLATE_PLACEHOLDER.search(prop[key])), None)
        if templated:
            problems.append({"element": uid, "rule": "placeholder",
                "reason": "prop." + templated + " keeps the unresolved placeholder "
                    + prop[templated].strip() + ", so the control has no literal accessible name",
                "fix": "Write the literal wording, or use prop.name_from when the name is a data value."})
            continue
        declared = prop.get("name_from")
        if declared is not None:
            if not isinstance(declared, str) or declared not in bindings:
                problems.append({"element": uid, "rule": "name_from",
                    "reason": "prop.name_from names no known binding",
                    "fix": "Point prop.name_from at the binding whose value is the record's name."})
            elif any(isinstance(prop.get(key), str) and prop[key].strip() for key in ("label", "text")):
                problems.append({"element": uid, "rule": "name_from",
                    "reason": "a control cannot take its accessible name from a value and from"
                        " static text at the same time; the name becomes a concatenation",
                    "fix": "Keep prop.name_from and render the static text in a separate element."})
            continue
        if node.get("type") in {"button", "link"} and not any(
                isinstance(prop.get(key), str) and prop[key].strip() for key in ("label", "text", "placeholder")):
            problems.append({"element": uid, "rule": "access_name",
                "reason": "this " + str(node.get("type")) + " has no accessible name, so no user or"
                    " reviewer can identify it",
                "fix": "Declare prop.text for its visible wording, or prop.name_from when the name is data."})
    for pid in sorted((design.get("pages") or {})):
        for uid, node in sorted(page_elements(design, pid).items()):
            if node.get("type") != "form":
                continue
            labels = [str((child.get("prop") or {}).get("label") or "").strip()
                for child in elements_of(design, uid).values()]
            duplicates = sorted({label for label in labels if label and labels.count(label) > 1})
            if duplicates:
                problems.append({"element": uid, "rule": "label",
                    "reason": "the form repeats the label(s) " + ", ".join(duplicates)
                        + ", so a field cannot be addressed by its label",
                    "fix": "Give every field a unique visible label."})
    return problems


def accessibility_findings(design: dict, uids) -> list[dict]:
    """Report the accessibility defects inside the subtree a proposal just wrote.

    Scoping to the written subtree keeps a fresh proposal from being rejected for a control it
    never touched: the pass that wrote that control already had to satisfy the same contract,
    and re-reporting it here would loop a repair against an unrelated page.
    """
    scoped = set(uids)
    for uid in list(scoped):
        scoped.update(elements_of(design, uid))
    return [problem for problem in accessibility_problems(design) if problem["element"] in scoped]


def elements_of(design: dict, uid: str) -> dict:
    """Return the subtree below one element, excluding the element itself."""
    selected = {uid}
    while True:
        expanded = selected | {child for child, node in (design.get("elements") or {}).items()
            if node.get("parent") in selected}
        if expanded == selected:
            return {child: design["elements"][child] for child in expanded - {uid}}
        selected = expanded


def owners(design: dict, pid: str) -> list[str]:
    """Return requirements that directly reference elements on this page."""
    ids = set(page_elements(design, pid))
    return [rid for rid, bound in design["requirements"].items() if ids.intersection(bound)]


def visible(design: dict, uid: str, values: dict) -> bool:
    """Evaluate the supported static/variable-equality visibility rule using the supplied display values."""
    visited = set()
    while uid is not None and uid not in visited and uid in design["elements"]:
        visited.add(uid)
        node = design["elements"][uid]
        rule = node.get("prop", {}).get("visible", True)
        if rule is False or (isinstance(rule, dict) and values.get(rule.get("var")) != rule.get("equals")):
            return False
        uid = node.get("parent")
    return True


def normalize_design(design, tree):
    """Fill optional display metadata; accept model decisions without design gates."""
    for pid, page in design["pages"].items():
        page.setdefault("name", pid)
        page.setdefault("images", [])
        page.setdefault("fixtures", {"default": {}})
    for node in design["elements"].values():
        node.setdefault("mode", [])
        node.setdefault("data", [])
        node.setdefault("prop", {})
    for kind, expected in (("requirements", tree.requirements), ("steps", tree.steps)):
        for identity in expected:
            design[kind].setdefault(identity, [])
    return design


def apply_proposal(design, proposal, rid, tree):
    """Replace supplied JSON records without retrieval, immutable-identity or UI-policy gates."""
    updated = deepcopy(design)
    for kind in ("pages", "elements", "bindings"):
        updated[kind].update(deepcopy(proposal[kind]))
    updated["requirements"][rid] = proposal["requirement_ui"]
    updated["steps"].update(deepcopy(proposal["step_ui"]))
    normalize_design(updated, tree)
    return updated

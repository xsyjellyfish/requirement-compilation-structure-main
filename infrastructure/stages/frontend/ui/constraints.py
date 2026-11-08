"""Turn requirement wording into verifiable UI control records.

A parent requirement may describe presentation that every page in its subtree must keep: the
account links in a header, a named submit button, a footer navigation. Requirement prose does
not survive into the generated UI by itself, so the wording is parsed once into
``{role, name, href}`` records. Later stages consume those records as data: the design request
names them, the design gate rejects a design that substitutes them, and the browser suites
assert the same role, accessible name and href. Parsing is deterministic and product-agnostic;
nothing here knows which site the requirement describes.
"""

import json
import re

ROLES = {"link": "link", "links": "link", "hyperlink": "link", "hyperlinks": "link",
    "button": "button", "buttons": "button", "checkbox": "checkbox", "checkboxes": "checkbox",
    "链接": "link", "按钮": "button", "复选框": "checkbox"}

# ASCII guards on purpose: `\w` also covers Han characters, which would reject "“登录”按钮和".
ROLE = re.compile(r"(?<![A-Za-z0-9_-])(links?|hyperlinks?|buttons?|checkbox(?:es)?|链接|按钮|复选框)(?![A-Za-z0-9_-])", re.I)

# "is labeled", "with the accessible name", "named", "显示为" introduce the exact user-visible name.
NAMED = re.compile(r"^\s*(?:is|are|must be|should be|was|were)?\s*"
    r"(?:labeled|labelled|named|with (?:the )?accessible name|with (?:the )?name|with text|显示为|名称为|名为)"
    r"\s*[\"“'「『`]([^\"”'」』`]{1,160})[\"”'」』`]", re.I)

HREF = re.compile(r"(?:whose|with)\s+href\s*(?:is|:|=|of)?\s*[\"']?(/[^\s,，;；。)\]}'\"”’]*)", re.I)
# An explicit label is itself a statement of requirement, so it needs no "show"/"display" verb.
LABEL = re.compile(r"\b(?:labeled|labelled|named)\b|accessible name|with (?:the )?name\b|显示为|名称为|名为", re.I)
AFFIRMATIVE = re.compile(r"\b(?:show|shows|display|displays|render|renders|provide|provides|include|"
    r"includes|offer|offers|present|presents|contain|contains|has|have|host|hosts)\b|显示|提供|包含|展示", re.I)
NEGATIVE = re.compile(r"\b(?:not|never|without|hide|hides|hidden|hiding|remove|removes|omit|omits|"
    r"disabled|invisible)\b|不|没有|隐藏", re.I)
LEAD = re.compile(r"^(?:(?:show|shows|display|displays|render|renders|provide|provides|include|includes|"
    r"offer|offers|present|presents|contain|contains|having|has|have|including|and|or|plus|both|"
    r"also|still|then|see|visible|a|an|the|of|with|as|it|its)\b|其|一个)[\s,:：]*", re.I)
# A determiner marks where a control's own name starts: "the site shows a Help link" -> "Help".
DETERMINER = re.compile(r"(?:^|\s)(?:a|an|the|its|其|一个|该)\s+", re.I)
QUOTE_NAME = re.compile(r"[\"“「『`]([^\"”」』`]{1,160})[\"”」』`]")
SPLIT = re.compile(r"\s*(?:,|，|、|&|\+|/|\band\b|\bor\b|及|和)\s*", re.I)
REJECT = re.compile(r"(?:href|role\s*=|https?:|\.\.|/|\b(?:the|this|that|these|those|both|all|each|"
    r"link|links|button|buttons|checkbox|checkboxes|page|pages|site|header|footer|nav|navigation|"
    r"here|below|above|following|previous|them|it|user|users|visitor|visitors|module|feature|"
    r"requirement|requirements|description|reference|image)\b)", re.I)
SENTENCE = re.compile(r"[^.;!?。；！？\n]+")
# Quoted spans hold a control's exact name, so they must survive sentence splitting. Apostrophes
# are excluded on purpose: an English possessive would otherwise pair with a later apostrophe and
# swallow whole sentences.
QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|「[^」]*」|『[^』]*』|`[^`]*`")


def _strip_lead(text: str) -> str:
    """Remove the leading verb and article words that precede a control's own name."""
    while True:
        stripped = LEAD.sub("", text, count=1)
        if stripped == text:
            return text
        text = stripped


def _name(value: str, quoted: bool = False) -> str | None:
    """Accept a name a reviewer could type verbatim, rejecting phrases that describe a role."""
    name = " ".join(str(value).split()).strip("\"'“”‘’「」『』`").strip(" :;,-–—")
    if not name or len(name) > (160 if quoted else 48):
        return None
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", name):
        return None
    if quoted:
        return name
    if len(name.split(" ")) > 6 or REJECT.search(name):
        return None
    return name


def _protect(text: str):
    """Replace quoted spans with placeholders so a name holding separators stays whole."""
    spans = []

    def keep(match):
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    return QUOTED.sub(keep, str(text or "")), spans


def _restore(text: str, spans: list) -> str:
    """Put the quoted spans back after splitting."""
    return re.sub(r"\x00(\d+)\x00", lambda match: spans[int(match.group(1))], text)


def sentences(text: str) -> list[str]:
    """Split wording into sentences, keeping a quoted control name intact."""
    protected, spans = _protect(text)
    found = []
    for sentence in re.split(r"[.;!?。；！？\n]+", protected):
        value = _restore(sentence, spans).strip()
        if value:
            found.append(value)
    return found


def fragments(text: str) -> list[str]:
    """Split wording into sentence and clause units without breaking a quoted control name."""
    protected, spans = _protect(text)
    units = []
    for sentence in re.split(r"[.;!?。；！？\n]+", protected):
        units.extend(re.split(r"[，,、]", sentence))
    found = []
    for unit in units:
        value = _restore(unit, spans).strip()
        if value:
            found.append(value)
    return found


def _labelled(value: str) -> bool:
    """A control's name looks like the text printed on it, not like a noun phrase describing it."""
    first = value[:1]
    return bool(re.match(r"[A-Z\u4e00-\u9fff]", first)) or not re.search(r"[a-z]", value)


def _names_from(lead: str) -> list[str]:
    """Read a control's own name from the wording that precedes the role word."""
    quoted = QUOTE_NAME.findall(lead)
    if quoted:
        value = _name(quoted[-1], quoted=True)
        return [value] if value else []
    cut = None
    for match in DETERMINER.finditer(lead):
        cut = match.end()
    if cut is not None:
        value = _name(lead[cut:])
        if value and _labelled(value):
            return [value]
    names = []
    for part in SPLIT.split(lead):
        value = _name(_strip_lead(part))
        if value and _labelled(value) and value not in names:
            names.append(value)
    return names


def _clause_controls(clause: str, affirmative: bool) -> list[dict]:
    """Read one clause's controls: an exact name, an inferred name, and an href when stated."""
    if not affirmative and not LABEL.search(clause):
        return []
    records, cursor = [], 0
    matches = list(ROLE.finditer(clause))
    for number, match in enumerate(matches):
        role = ROLES[match.group(1).lower()]
        lead = clause[cursor:match.start()]
        stop = matches[number + 1].start() if number + 1 < len(matches) else len(clause)
        window = clause[match.end():stop]
        cursor = match.end()
        names, href = [], None
        named = NAMED.match(window)
        if named:
            value = _name(named.group(1), quoted=True)
            names = [value] if value else []
        if not names and not named:
            if NEGATIVE.search(lead) or NEGATIVE.search(window):
                continue
            described = re.sub(r"(?:whose|with)\s+href\s*(?:is|:|=)?\s*\S+\s*", " ", lead, flags=re.I)
            names = _names_from(described)
        found = HREF.search(window)
        if found and role == "link":
            href = found.group(1).rstrip(".,;:)]}'\"”’")
        for name in names:
            record = {"role": role, "name": name}
            if href:
                record["href"] = href
            if record not in records:
                records.append(record)
    return records


def text_controls(text: str) -> list[dict]:
    """Return the controls one requirement text declares, in reading order."""
    records = []
    for sentence in sentences(text):
        affirmative = bool(AFFIRMATIVE.search(sentence)) or bool(LABEL.search(sentence))
        for clause in fragments(sentence):
            for record in _clause_controls(clause, affirmative):
                if record not in records:
                    records.append(record)
    return records


def _key(record: dict) -> tuple:
    """Identify one control by role and case-insensitive name so repeated wording collapses."""
    return record["role"], " ".join(record["name"].split()).casefold()


def requirement_controls(tree) -> list[dict]:
    """Index every declared control once, keeping the requirements that declare it."""
    controls, index = [], {}
    for rid, requirement in tree.requirements.items():
        parts = [requirement.get("name"), requirement.get("description")]
        for sid in tree.own_steps.get(rid, []):
            step = tree.steps.get(sid) or {}
            parts.extend(step.get(key) for key in ("Given", "When", "Then"))
        records = []
        for part in parts:
            if not isinstance(part, str):
                continue
            for record in text_controls(part):
                if record not in records:
                    records.append(record)
        for record in records:
            key = _key(record)
            if key in index:
                known = index[key]
                if record.get("href") and not known.get("href"):
                    known["href"] = record["href"]
                if rid not in known["sources"]:
                    known["sources"].append(rid)
                continue
            known = {"id": f"UIC-{len(controls) + 1:02d}", **record, "sources": [rid], "element": None}
            index[key] = known
            controls.append(known)
    return controls


def parents(tree) -> dict:
    """Map every requirement to its containing requirement."""
    return {child: rid for rid, children in tree.children.items() for child in children}


def ancestors(tree, rid: str) -> list[str]:
    """Return the requirement's containers from its parent up to the root."""
    known, chain = parents(tree), []
    current = known.get(rid)
    while current is not None and current not in chain:
        chain.append(current)
        current = known.get(current)
    return chain


def required_controls(tree, controls: list[dict], rid: str) -> list[dict]:
    """Return the controls this requirement and its containers require, marking inherited ones."""
    chain, containers = [rid, *ancestors(tree, rid)], ancestors(tree, rid)
    selected = []
    for control in controls:
        sources = set(control["sources"])
        if not sources.intersection(chain):
            continue
        selected.append({**control, "inherited": bool(sources.intersection(containers)),
            "required_by": next(source for source in control["sources"] if source in chain)})
    return selected


def _normalized(value) -> str:
    """Compare a user-visible name or URL without regard to repeated spacing or letter case."""
    text = " ".join(str(value or "").split()).casefold()
    return text.rstrip("/") or "/"


def accessible_name(design: dict, node: dict) -> str | None:
    """Read the accessible name a rendered control would expose to a user or reviewer."""
    prop = node.get("prop") or {}
    for key in ("label", "text"):
        value = prop.get(key)
        if isinstance(value, str) and value.strip():
            return value
    declared = prop.get("name_from")
    if isinstance(declared, str):
        initial = ((design.get("bindings") or {}).get(declared) or {}).get("initial")
        if isinstance(initial, str) and initial.strip():
            return initial
    return None


def matching(design: dict, pid: str, control: dict) -> str | None:
    """Return the element on one page that renders a required control, if any."""
    from infrastructure.stages.frontend.ui.design import page_elements
    for uid, node in page_elements(design, pid).items():
        if node.get("type") != control["role"]:
            continue
        name = accessible_name(design, node)
        if not name or _normalized(name) != _normalized(control["name"]):
            continue
        if control["role"] == "link" and control.get("href"):
            if _normalized((node.get("prop") or {}).get("href")) != _normalized(control["href"]):
                continue
        return uid
    return None


def page_ids(design: dict, rid: str) -> list[str]:
    """Return the pages whose UI subtree one requirement uses."""
    from infrastructure.stages.frontend.ui.design import page_elements
    bound = set((design.get("requirements") or {}).get(rid) or [])
    if not bound:
        return []
    return [pid for pid in sorted(design.get("pages") or {}) if bound.intersection(page_elements(design, pid))]


def unmet_controls(design: dict, tree, controls: list[dict], rid: str) -> list[dict]:
    """Report the controls one requirement must show that the design does not render.

    A parent requirement's wording owns presentation that its whole subtree keeps, so the
    finding also covers inherited controls. Every finding names the exact role, name and href
    the browser suite will assert, which turns a wording constraint into a design obligation
    instead of a suggestion a later stage can silently substitute.
    """
    problems = []
    pages = page_ids(design, rid) or sorted(design.get("pages") or {})
    for control in required_controls(tree, controls, rid):
        if any(matching(design, pid, control) for pid in pages):
            continue
        problems.append({"control": control["id"], "role": control["role"], "name": control["name"],
            **({"href": control["href"]} if control.get("href") else {}),
            "inherited": control["inherited"], "required_by": control["required_by"],
            "reason": "this requirement declares a " + control["role"] + " named "
                + repr(control["name"]) + (", linked to " + control["href"] if control.get("href") else "")
                + ", and the design renders it nowhere on the pages this requirement uses",
            "fix": "Add a " + control["role"] + " element whose accessible name is exactly "
                + repr(control["name"]) + (", with prop.href " + repr(control["href"]) if control.get("href") else "")
                + (". It is inherited from requirement " + control["required_by"]
                    + ", so repeat it as shared header/footer content on this page." if control["inherited"] else ".")
                + " Never rename it, split its name across several elements or replace it with text."})
    return problems


def required_controls_for_page(design: dict, tree, pid: str, controls: list[dict] | None = None) -> list[dict]:
    """Return the controls every requirement using this page requires, with their element IDs."""
    from infrastructure.stages.frontend.ui.design import page_elements
    known = requirement_controls(tree) if controls is None else controls
    elements = set(page_elements(design, pid))
    selected = []
    for rid in tree.requirements:
        if tree.children[rid] or not elements.intersection((design.get("requirements") or {}).get(rid) or []):
            continue
        for control in required_controls(tree, known, rid):
            if any(seen["id"] == control["id"] for seen in selected):
                continue
            selected.append({**control, "page": pid, "element": matching(design, pid, control)})
    return selected


class ConstraintError(ValueError):
    """Attach the concrete design conflicts so a retry can repair them."""
    def __init__(self, problems: list[dict]):
        super().__init__("The design is incomplete or unaddressable: "
            + json.dumps(problems, ensure_ascii=False))
        self.feedback = {"problems": problems,
            "repair": "Render every listed control with exactly that HTML role, accessible name and"
                " href. Inherited controls come from a parent requirement, so repeat them as shared"
                " header/footer content on this page even though this requirement never restates them."
                " Point every prop.image at a supplied image ID listed in the requirement evidence."
                " A control with no accessible name cannot be found by a user or a reviewer: give it"
                " prop.text, or prop.name_from when the name is a data value, and never leave a"
                " template placeholder such as \"{record.title}\" in prop.text, prop.label or"
                " prop.placeholder."}


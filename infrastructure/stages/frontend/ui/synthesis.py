"""Step 09 orchestration: retrieve pages, propose UI/data, parse, and publish.

All leaf requirements are designed before step 10 implements any UI. Reuse is
page-first: retrieval returns the corresponding UI/data subtree, not disconnected
components. Prompts and design records live in ui/design.py."""

import json

from infrastructure.stages.frontend.ui.constraints import (ConstraintError, required_controls,
    requirement_controls, unmet_controls)
from infrastructure.stages.frontend.ui.design import (INSTRUCTIONS, accessibility_findings,
    apply_proposal, empty_design, page_elements, normalize_design, page_context, UIResponseError)
from infrastructure.stages.frontend.ui.proposals import INSTRUCTIONS as EXAMPLE_INSTRUCTIONS
from infrastructure.shared.rag.pages import PageRetriever
from infrastructure.utils.files import save_json
from infrastructure.shared.agent.proposals import propose_json


def bind_image_sources(design, images):
    """Point every designed image element at the URL that serves its supplied image.

    A design names a supplied image by ID; the compiler owns the URL, so binding it here keeps a
    model from guessing a path that the static host never serves. An unknown ID is a finding
    rather than a silent broken frame.
    """
    records = (images or {}).values() if isinstance(images, dict) else (images or [])
    served = {image.get("id"): image.get("url") for image in records if isinstance(image, dict)}
    problems = []
    for uid, node in design["elements"].items():
        if node.get("type") != "image":
            continue
        declared = (node.get("prop") or {}).get("image")
        if declared is None:
            continue
        url = served.get(declared)
        if url:
            node["prop"]["src"] = url
            continue
        problems.append({"element": uid, "image": declared,
            "reason": "the image element copies image " + json.dumps(declared, ensure_ascii=False)
                + ", which this input does not supply",
            "fix": "Use one of the supplied image IDs " + json.dumps(sorted(served), ensure_ascii=False)
                + " in prop.image, or remove the image element when the page has no supplied content image."})
    return problems


def bind_requirement_image_pages(design, requirement, rid):
    """Bind image-backed pages to the current requirement when the page hint is unambiguous."""
    owned = list(design["requirements"].get(rid, []))
    images = requirement.get("images", {})
    image_records = images.values() if isinstance(images, dict) else images
    for image in image_records:
        if not isinstance(image, dict):
            continue
        image_id, page_hint = image.get("id"), image.get("page")
        # The requirement's explicit page hint is authoritative. The model may
        # omit the redundant page.images entry when creating a new page.
        if page_hint in design["pages"]:
            page = design["pages"][page_hint]
            page.setdefault("images", [])
            if image_id not in page["images"]:
                page["images"].append(image_id)
            candidates = [page_hint]
        else:
            candidates = [pid for pid, page in design["pages"].items() if image_id in page.get("images", [])]
        if len(candidates) != 1:
            continue
        root = design["pages"][candidates[0]]["root"]
        if root in design["elements"]:
            if root not in owned:
                owned.append(root)
    design["requirements"][rid] = owned


def synthesize_ui(arc_dir, tree, images, contexts, model, console, top_k=3, max_fixtures=3, max_attempts=10):
    """Design UI/data for all leaf requirements using page-first retrieval.

    Show existing/retrieved pages, ask for an incremental proposal, and feed
    response-consumption errors back for repair. Save accepted ui_design.json
    updates; implementation and browser tests belong to step 10."""
    design, retriever = empty_design(), PageRetriever()
    controls = requirement_controls(tree)
    for rid in tree.requirements:
        if tree.children[rid]:
            design["requirements"][rid] = []
            for sid in tree.own_steps[rid]:
                design["steps"][sid] = []
            console.show(f"{rid} | Skip non-leaf UI/data analysis", "Only leaf requirements are analyzed. Organizational parents do not generate or inherit UI nodes.")
    for number, rid in enumerate(tree.leaves, 1):
        requirement = contexts[rid]
        label = f"UI/data design {number}/{len(tree.leaves)} {rid}"
        console.show(label + " | 1. Requirement and steps", json.dumps(requirement, ensure_ascii=False, indent=2))
        console.show(label + " | 2. All Existing Pages", json.dumps(design["pages"], ensure_ascii=False, indent=2))
        retrieved = retriever.retrieve(requirement, design, top_k)
        console.show(label + " | 3. Retrieved page subtrees (RAG)", json.dumps(retrieved, ensure_ascii=False, indent=2))
        request = {
            "requirement": requirement, "retrieved_pages": retrieved["pages"],
            "all_pages": {pid: {key: page[key] for key in ("name", "route", "root", "images")}
                for pid, page in design["pages"].items()},
            "required_controls": [{key: control[key] for key in ("id", "role", "name", "href", "inherited", "required_by")
                if control.get(key)} for control in required_controls(tree, controls, rid)],
            "max_fixtures": max_fixtures,
        }
        def accept(proposal):
            try:
                updated = apply_proposal(design, proposal, rid, tree)
            except (KeyError, TypeError, AttributeError) as error:
                raise UIResponseError(error, proposal) from error
            bind_requirement_image_pages(updated, requirement, rid)
            problems = (bind_image_sources(updated, images) + unmet_controls(updated, tree, controls, rid)
                + accessibility_findings(updated, proposal["elements"]))
            if problems:
                raise ConstraintError(problems)
            for pid in proposal["pages"]:
                console.show(label + f" | Page context {pid}", json.dumps(page_context(updated, pid), ensure_ascii=False, indent=2))
            return updated

        updated, proposal = propose_json(model, INSTRUCTIONS + "\n\n" + EXAMPLE_INSTRUCTIONS,
            request, accept, console, label, max_attempts)
        console.show(label + " | 4. Incremental UI/data changes", json.dumps(proposal, ensure_ascii=False, indent=2))
        design = updated
        save_json(arc_dir / "ui_design.json", design)
        console.artifact("ui_design", design, label + " | Accepted UI")
    normalize_design(design, tree)
    save_json(arc_dir / "ui_design.json", design)
    console.show("UI/data design complete for ALL requirements", f"Saved {arc_dir / 'ui_design.json'}\nRun 10_implement_ui.py for existence tests and UI implementation. Interactions belong to step 11.")
    return design

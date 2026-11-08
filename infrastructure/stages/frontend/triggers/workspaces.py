"""Supply the smallest application surface a requirement's E2E task and repair need.

A requirement owns its triggers, the pages that host them and the responses of the APIs
those triggers call. Everything else in ``frontend/src`` and ``backend/src`` is catalogue
only: ``SourceFiles.feedback`` adds any file a failing check actually names, so an
unrelated helper still becomes available in the round that needs it. Supplying both whole
source trees reached ~90 KB (~35 000 tokens) per request, three times the run mean.
"""

from infrastructure.shared.project.entrypoints import selected_path
from infrastructure.stages.frontend.triggers.e2e_runtime import FIXTURE

RUNTIME = "frontend/src/ui-runtime.ts"
BACKEND_SUPPORT = "backend/tests/support/context.ts"
ROUTE_REGISTRATION = "backend/src/api/register.ts"
STYLE = "frontend/src/style.css"


def called_apis(context):
    """Return the canonical API IDs the supplied triggers declare."""
    return {action["api"] for trigger in context["triggers"].values()
        for action in trigger.get("action") or [] if action.get("kind") == "api"}


def handler_paths(output, api_ids):
    """Return the accepted handler file of every API the requirement calls."""
    return {path for aid in sorted(api_ids)
        if (path := selected_path(output, "api", aid)) and (output / path).is_file()}


def page_paths(selection, page_ids):
    """Resolve the implementation file of each page.

    When the path index has no entry for a page (for example when the acceptance layer
    runs on its own), every editable frontend source is offered instead: an unknown
    rendering file must never become unreachable for repair.
    """
    resolved = selection.paths("page", sorted(page_ids))
    if resolved or not page_ids:
        return resolved
    return set(selection.editable) - {STYLE}


def application_paths(selection, output, context):
    """The requirement's pages and the backend handlers its own triggers call."""
    return {*page_paths(selection, context["page_subtrees"]),
        *handler_paths(output, called_apis(context))}


def triggers_for_pages(ui, design, page_ids):
    """Return every trigger whose source element lives on one of the given pages."""
    elements = ui.get("elements") or {}
    roots = {ui["pages"][pid].get("root") for pid in page_ids if pid in (ui.get("pages") or {})}

    def on_page(uid):
        """Walk the element's parents; a trigger belongs to the page that renders it."""
        current, visited = uid, set()
        while current in elements and current not in visited:
            if current in roots:
                return True
            visited.add(current)
            current = elements[current].get("parent")
        return current in roots

    return {tid for tid, trigger in (design.get("triggers") or {}).items()
        if on_page(trigger.get("source"))}


def test_workspace(selection, output, context, rid, relative):
    """Offer the suite target plus the application files the scenario is written against.

    The suite is written against the requirement's pages and the destination pages its
    design mentions, because a navigation assertion reads a page the requirement does
    not own. These files stay read-only: a test may never repair the application.
    """
    return selection.workspace(target=("e2e_test", rid), existing=relative,
        references=[RUNTIME, FIXTURE, BACKEND_SUPPORT,
            *sorted(application_paths(selection, output, context))])


def implementation_workspace(selection, backend_selection, output, context, trigger_ids, test_relative):
    """Supply the requirement's triggers, pages, their local imports and its API handlers.

    Pages and style stay writable so an inaccessible control or a missing binding can be
    repaired; handlers of the called APIs stay writable so an integration defect in the
    contract the requirement uses is repairable; the suite and runtime wiring do not.
    """
    handlers = handler_paths(output, called_apis(context))
    workspace = selection.workspace(triggers=set(trigger_ids),
        paths={*page_paths(selection, context["page_subtrees"]), STYLE},
        references=[test_relative, RUNTIME, BACKEND_SUPPORT, ROUTE_REGISTRATION,
            *sorted(application_paths(selection, output, context))])
    backend_selection.include(workspace, handlers)
    return workspace

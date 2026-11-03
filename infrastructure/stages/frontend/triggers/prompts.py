"""Trigger design: native source, frontend data dependencies and ordered actions."""

INSTRUCTIONS = """Design complete frontend triggers for the supplied requirement using ui, backend_apis
and existing_triggers. Return {triggers:{}, requirement_triggers:[], step_triggers:{}}.
triggers maps trigger IDs to COMPLETE new or changed records with these separate fields:
- source: the emitting UI ELEMENT ID.
- event: mount, click, input, change, submit, keydown or keyup, captured by the host.
- guard: an enabling condition in plain language, or null.
- read: array of frontend variable names consumed.
- write: array of frontend variable names updated.
- action: an ORDERED ARRAY of actions, not an event/handler ID.
Complete response example; substitute supplied IDs and behavior:
{
  "triggers": {
    "TR-save": {
      "source": "UI-form",
      "event": "submit",
      "guard": null,
      "read": [
        "form.text"
      ],
      "write": [
        "form.error"
      ],
      "action": [
        {
          "kind": "api",
          "api": "API-save",
          "description": "Submit form.text; show the rejection message or continue after success"
        }
      ]
    }
  },
  "requirement_triggers": [
    "TR-save"
  ],
  "step_triggers": {
    "STEP-save": [
      "TR-save"
    ]
  }
}

Each action contains kind and description. An API action uses kind="api" and api=API_ID.
A local UI action uses kind="ui" and ui=ELEMENT_ID for updates, feedback or navigation.
For navigation, reference the destination page's root element from ui.pages; it identifies
the page that implementation later passes to context.navigate by PAGE ID.
Describe action conditions, request sources, response branches and transformations in
prose. Guards and actions are implemented as code, not interpreted as an expression DSL.

For every requirement scenario, establish a complete interaction chain:
UI event -> current input bindings -> existing API ->
success/error response -> visible UI state or navigation.

Every required WHEN must be executable through an actual UI control.
Every required THEN must have an observable result.

Use only supplied UI IDs, bindings and API contracts.
On failure, preserve the correct page and display the required error.
On success, update the appropriate state before navigation.

Reuse existing triggers and APIs rather than introducing duplicates.
Do not create redundant input/change triggers for native bindings.

SCENARIO COVERAGE: map every relevant WHEN to a genuine event on an actual UI
control and map every specified THEN to a visible state update, navigation, or
API result. For each API action identify exactly which existing bindings populate
params/query/body; on success describe the required follow-up UI state, and on
failure describe its visible error state and absence of false success/navigation.
If a requirement includes session restoration after reload, attach a mount action
using the existing current-session API; if it includes logout, ensure subsequent
signed-out display. Never fabricate API IDs, responses, or session state.

The host synchronizes native editable values before business triggers. Add input/change
triggers for additional behavior such as filtering, not routine value copying. A mount
trigger represents page entry; use that page's root element as its source.
Prefer one submit/click trigger that implements both success and error branches over
separate redundant event handlers. Native bindings already track ordinary input.
Reuse trigger IDs where behavior is shared and preserve earlier behavior when updating.
requirement_triggers associates this requirement's triggers; step_triggers maps scenario
steps to trigger lists, using [] for static/native behavior. Non-UI behavior needs no trigger.

The read and write arrays name FRONTEND variables, not DB fields. DB effects are derived
from called APIs. Return only trigger definitions and requirement/step associations.
Expected business outcomes come
from the requirement; API contracts supply transport and persistence behavior.
"""

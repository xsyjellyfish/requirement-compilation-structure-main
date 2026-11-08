"""Step 06: design complete APIs with interfaces, obligations and P/Q."""

import json
from contextlib import ExitStack
from pathlib import Path

from infrastructure.stages.backend.context import load_context, requirement_context, api_requirement_uses
from infrastructure.stages.backend.contracts import creates_by_entity, validate_contracts
from infrastructure.stages.backend.primitives import apply_decomposition, read_api_proposal, validate_design
from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.shared.models.text import TextModel
from infrastructure.stages.database.obligations.rules import union
from infrastructure.utils.presentation.formatting import format_apis
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.logging import console_log
from infrastructure.utils.presentation.formatting import format_obligations
from infrastructure.shared.rag.apis import ApiRetriever
from infrastructure.utils.files import save_json
from infrastructure.shared.agent.proposals import propose_json


INSTRUCTIONS = """Design complete backend APIs for the supplied leaf requirement and scenarios.
Design each operation's signature, input, output, DB reads, DB writes, preconditions
and postconditions together. Return {"apis": {"API-ID": {...}}, "requirement_apis": []}.
Both keys are top-level sections of that one object: apis maps API IDs to definitions and
requirement_apis is its sibling array, never a member of apis.
apis contains COMPLETE new or changed definitions; reuse unchanged APIs by ID only.
Each API has these separate top-level properties:
- description: the business operation.
- read: array of qualified DB fields read by the operation.
- write: array of qualified DB fields written by the operation.
- signature: {name: TypeScript function name, method: HTTP method, path: /api/...}.
  A path starts with "/" and writes every route parameter as {name}, for example
  "/api/books/{bookTitle}/pages/{pageTitle}"; declare each one in request.params.
- request: {params: JSON schema, query: JSON schema, body: JSON schema, headers: JSON schema}.
- response: {success: {status, body: JSON schema, headers: JSON schema},
  errors: [{status, body: JSON schema, headers: JSON schema}]}.
- precondition: [{id, description, error_status}], or [] for an unconditional operation.
- postcondition: [{id, description, status}], describing observable response and DB
  outcomes, including unchanged data on rejection when required.

Complete response example; substitute actual requirement evidence and names:
{
  "apis": {
    "API-list-notes": {
      "description": "List persisted notes",
      "read": [
        "Note.text"
      ],
      "write": [],
      "signature": {
        "name": "listNotes",
        "method": "GET",
        "path": "/api/notes"
      },
      "request": {
        "params": {
          "type": "object"
        },
        "query": {
          "type": "object"
        },
        "body": {
          "type": "object"
        },
        "headers": {
          "type": "object"
        }
      },
      "response": {
        "success": {
          "status": 200,
          "body": {
            "type": "object",
            "properties": {
              "notes": {
                "type": "array",
                "items": {
                  "type": "string"
                }
              }
            },
            "required": [
              "notes"
            ]
          },
          "headers": {
            "type": "object"
          }
        },
        "errors": []
      },
      "precondition": [],
      "postcondition": [
        {
          "id": "Q-notes",
          "description": "Return persisted note texts without changing records",
          "status": 200
        }
      ]
    }
  },
  "requirement_apis": [
    "API-list-notes"
  ]
}

Use inline JSON Schemas for I/O, with object properties/required and array items where
appropriate. Declare route parameters in params. Use lower-case header names and
state any shared credential/cookie convention consistently across related APIs.
Response bodies expose business results, not password hashes or session secrets.
Specify success and rejection branches supported by the requirement; HTTP 501 is
reserved for the TDD stub. Do not invent business policy or generic failure branches.

STATIC CONTRACT RULES; a proposal is rejected with the failing API ID and field when
any of these does not hold:
- signature.method is one of GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS.
- signature.path is absolute, for example "/api/books/{bookTitle}". "{name}" and
  ":name" are equivalent, every parameter has its own name, and no path carries "?",
  "#", whitespace or an empty segment.
- request.params.properties declares exactly the parameters of signature.path, and no
  others. A path without parameters declares none.
- signature.name is a unique TypeScript identifier, API IDs are unique ignoring case,
  and each method+path pair appears once; two routes that differ only in a parameter
  name are ambiguous.
- every requirements key exists in requirements.json, and every API is listed by a
  requirement in requirement_apis.
- request.params/query/body/headers and response.success/errors are object schemas;
  "required" names only declared properties.
- response.success.status is 2xx; every response.errors[].status is 4xx or 5xx.
- precondition and postcondition are arrays, [] when unused.

ACCEPTANCE-CENTRIC DESIGN: for every explicit scenario, ensure a real caller can
perform its WHEN through an API and observe its THEN in response data and/or
persisted state. Include missing/invalid/conflicting-input rejection paths when
required; rejected operations must not create partial records or sessions.
Use stable field names and HTTP contracts suitable for actual browser interaction.
For sign-in/session/refresh/sign-out requirements, design one consistent credential
transport and session lifecycle across reused APIs; do not make the UI guess a token
or user identifier. Reuse contracts rather than inventing near-duplicate endpoints.
Check the read/write union against the supplied obligations before returning JSON.

SEMANTIC REUSE RULES; a proposal is rejected when any of these does not hold:
- One create per entity. An entity is created by exactly one POST: the operation whose
  write array covers most of its columns. When this requirement needs an existing record
  created, list the creating API ID in requirement_apis instead of designing a second,
  narrower create for the same table. created_entities names the API that already creates
  each entity.
- A create accepts every business field the requirement makes mandatory. Never drop a
  field an earlier create of that entity required, never turn one of them into an optional
  member, and never omit a column its write array persists: a narrower duplicate lets a
  caller, a test or the UI skip a field the requirement demands.
- When the create that already exists accepts fewer fields than this requirement makes
  mandatory, return the complete updated definition of that API with the missing body
  fields and persisted columns added, and list its ID in requirement_apis. Never keep a
  second create for the entity, and never let a caller, a test or the UI supply a field the
  accepted create does not declare.
- Every required request.body member must be sendable from the contract alone: declare a
  typed scalar (string, number, integer, boolean), or a const, default, examples or a
  non-empty enum, or an object/array of such members. A required member whose value the
  caller cannot derive is rejected, because no client can satisfy it.

One requirement may need several APIs, and one API may serve several requirements. Record
that many-to-many relation in requirement_apis: the same API ID appearing under several
requirements is the intended way to reuse a contract, and duplicating the operation to
avoid sharing it is always wrong.

retrieved_apis contains existing complete definitions; api_uses contains their earlier
requirement scenarios. The current requirement is separate. A shared API has one
canonical interface and P/Q covering its known uses. Return a complete updated API
when new evidence requires coordinated interface or P/Q changes; retain earlier behavior.

requirement_apis lists APIs directly needed by this requirement. The union of their
read arrays must equal its read obligation, and likewise for write arrays. Shared API
updates must preserve coverage of earlier requirements. Effects can overlap across
APIs and describe possible effects across branches, not unconditional per-call access.
Use requirement_apis=[] for requirements with no backend behavior and empty obligations.
Return only API definitions and requirement associations.
"""


def design_backend_apis(arc_dir: Path, pause: bool = False, top_k: int = 6, max_attempts: int = 10) -> dict:
    tree, schema, obligations = load_context(arc_dir)
    if top_k < 1:
        raise ValueError("--top-k must be positive")
    console, retriever, model = DemoConsole(pause), ApiRetriever(), TextModel()
    # Parents organize requirements; they do not receive APIs or coverage checks.
    design = {"apis": {}, "requirements": {rid: [] for rid in tree.requirements if tree.children[rid]}}
    # --- Extension point 06A: leaf order and reusable API discovery ------------
    for number, rid in enumerate(tree.leaves, 1):
        context = requirement_context(tree, rid)
        required = obligations["requirements"][rid]
        label = f"{number}/{len(tree.leaves)} {rid}"
        console.show(f"{label} | 1. Requirement and Dataflow Obligations", (
            context["name"] + "\n" + context["description"] + "\n\n" + context["concatenated_steps"]
            + "\n\nRequirement obligations:\n" + format_obligations({rid: required})
        ))
        console.show(f"{label} | 2. All Existing APIs", format_apis(design["apis"]))
        query = json.dumps({"requirement": context, "obligations": required}, ensure_ascii=False)
        candidates = design["apis"]
        retrieved = retriever.retrieve(query, candidates, top_k)
        console.show(f"{label} | 3. Retrieved APIs (RAG)", (
            "\n".join(f"{item['symbol']} BM25={item['score']:.4f}" for item in retrieved["matches"])
            + "\n\n" + format_apis(retrieved["apis"])
        ))
        related = set(required["read"]) | set(required["write"])
        for api in retrieved["apis"].values():
            related.update(api["read"])
            related.update(api["write"])
        related_schema = {name: {"description": entity["description"],
            "fields": {field: value for field, value in entity["fields"].items() if f"{name}.{field}" in related}}
            for name, entity in schema["entities"].items()
            if any(symbol.startswith(name + ".") for symbol in related)}
        request = {
            "requirement": context, "obligations": required, "related_schema": related_schema,
            "retrieved_apis": retrieved["apis"], "api_inventory": list(candidates),
            # One entity, one create: the API that already creates each record is named here
            # so this requirement reuses it instead of adding a narrower duplicate.
            "created_entities": creates_by_entity(design),
            "api_uses": {aid: api_requirement_uses(tree, design["requirements"], aid)
                for aid in retrieved["apis"]},
        }
        def accept(response):
            # Read both sections first: a section the model nested elsewhere is reported
            # as that path and reused, instead of failing later as a missing key.
            proposal, notes = read_api_proposal(response, rid, known=design["apis"])
            if notes:
                console.show(f"{label} | Response sections normalized", "\n".join(notes))
            updated = apply_decomposition(design, proposal, rid, obligations, tree=tree)
            selected = {aid: updated["apis"][aid] for aid in updated["requirements"][rid]}
            definitions = {aid: {key: api[key] for key in (
                "signature", "request", "response", "read", "write", "precondition", "postcondition")}
                for aid, api in selected.items()}
            console.show(f"{label} | Complete API definitions", json.dumps(definitions, ensure_ascii=False, indent=2))
            console.show(f"{label} | API design / Obligation Propagation",
                format_apis(selected) + "\nRequired direct obligations:\n" + format_obligations({rid: required})
                + "\nAssigned API obligation union:\n" + format_obligations({rid: union(selected.values())}))
            return updated

        updated, _ = propose_json(model, INSTRUCTIONS, request, accept, console,
            f"Backend API design {rid}", max_attempts)
        design = updated
        save_json(arc_dir / "backend_apis.json", design)
        console.artifact("backend_apis", design, f"{label} | Accepted APIs")
    # --- Extension point 06B: API -> DB Access Primitive decomposition ---------
    # Intentionally absent: an API directly accesses the database in step 08.
    validate_design(design, obligations, tree=tree)
    validate_contracts(design, tree=tree)
    save_json(arc_dir / "backend_apis.json", design)
    print(f"Backend API Primitives saved: {arc_dir / 'backend_apis.json'}", flush=True)
    return design


def main() -> None:
    parser = create_parser("Step 06: Complete Backend API Design / Obligation Propagation")
    parser.add_argument("--top-k", type=int, default=6, help="Number of API retrieval hits")
    parser.add_argument("--max-attempts", type=int, default=10, help="Maximum proposals per requirement; including the first proposal (default: 10)")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            _, output = resolve_directories(args)
            stack.enter_context(console_log(output, debug_model_io=args.debug_model_io))
            design_backend_apis(output / ".arc", args.pause, args.top_k, args.max_attempts)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")


if __name__ == "__main__":
    main()

"""Step 02: discover entities and fields incrementally using schema RAG."""

import json
from contextlib import ExitStack
from pathlib import Path

from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.shared.models.text import TextModel
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.formatting import format_changes, format_schema
from infrastructure.utils.presentation.logging import console_log
from infrastructure.shared.rag.schema import SchemaRetriever
from infrastructure.stages.requirements.tree import RequirementTree
from infrastructure.stages.database.schema import apply_patch
from infrastructure.stages.database.initial_data import (
    SeedRejected, load_tree, seed_gaps, seed_prerequisites)
from infrastructure.utils.files import save_json
from infrastructure.shared.agent.proposals import propose_json


# --- Extension point 02A: schema discovery policy ------------------------------
# Experiment with entity boundaries, reuse decisions, and constraint inference.
# Keep the upsert response compatible with infrastructure/stages/database/schema.py.
INSTRUCTIONS = """Design persistent data for the supplied requirement and its scenario steps.
Reuse the supplied retrieved entities and symbol_inventory; extend existing entities
rather than introducing synonyms for the same concept. Preserve unrelated definitions.
Store persistent business data, not transient UI values such as password confirmation.
Treat every GIVEN as existing state, WHEN as an action, and THEN as the required
persistent outcome. Model only entities and fields needed to make those outcomes
possible. Prefer reusing an existing entity over creating a near-duplicate.
For identity/session flows, preserve the same user key and credential representation
across registration, login, logout and refresh; persist sessions only if required by
the scenarios. Never store raw passwords or confirmation fields.
Derive constraints from requirement evidence; example fixture values alone do not
establish uniqueness, normalization, value ranges or defaults.

Return {"entities": {...}, "related_fields": ["Entity.field"]}.
entities is an incremental update keyed by entity name. Each new entity contains
"description" (string) and "fields" (object keyed by field name).
Each new field contains these separate properties:
- description: string.
- type: string, integer, number, boolean, date, datetime, uuid or json.
- nullable, primary_key, unique: boolean properties.
- references: null or a qualified "Entity.field" string.
- constraints: array of descriptions of required value/domain constraints.
Use SQL-compatible entity/field identifiers. Represent keys and relationships needed
by the requirements, with compatible referenced field types. Password storage holds
hashes rather than plaintext. Do not invent unrelated business features.

Seed prerequisites ("Seed prerequisites (prepared before this scenario):" followed by
"- <kind>: {json}") state records that already exist when the scenario starts. Every
value of every declared prerequisite must have a home in a persisted field: model the
entity the prerequisite describes (an account, a profile, a shelf, ...), keep the declared
identity values in fields a lookup can match, and store a declared credential as a hash
field rather than a plaintext one. A schema that cannot store a declared prerequisite is
incomplete even when every WHEN/THEN is modelled.

For existing entities/fields, return only changed properties; omitted properties
are preserved by the compiler. Use references=null to clear a reference. Return
entities={} when existing definitions suffice. related_fields lists the qualified
persistent fields relevant to this requirement, including keys and relationships.
It is a schema association, not a read/write obligation. Use names from the resulting
schema.
"""

# --- Extension point 02E: seed storability repair ------------------------------
# Entity discovery owns the prerequisite data plane: a declared prerequisite that no
# entity can store is repaired here, before step 05 writes tables that cannot hold it.
SEED_INSTRUCTIONS = """Extend the schema so every declared Seed prerequisite value can be stored.
The supplied missing_storage entries name a prerequisite value that no entity supports, or a
prerequisite whose values do not fit one entity. Add or extend the entity the prerequisite
describes (an account, a profile, a shelf, ...) so every declared value has a persisted field:
keep the declared identity values in fields a lookup can match, and store a declared credential
as a hash field ("password_hash" or "hashed_password" documented as a hash) rather than a
plaintext "password" field. Reuse an entity that already stores the other declared values.
Do not remove, rename or redefine existing entities and fields, and do not model features the
prerequisites do not declare.

Return {"entities": {...}, "related_fields": ["Entity.field"]} in the same format as the schema
update. Each added field contains description, type, nullable, primary_key, unique, references
and constraints. Return entities={} only when the schema already stores every declared value.
"""


def requirement_nodes(root: dict):
    """Visit leaf requirements in document order; parents only organize the tree."""
    tree = RequirementTree(root)
    for rid in tree.leaves:
        yield {**tree.requirements[rid], "steps": tree.concatenated_steps(rid)}


def repair_seed_storability(arc_dir: Path, schema: dict, model, console, max_attempts: int,
        attempts: int = 2) -> dict:
    """Give the schema a home for every declared Seed prerequisite, before any DDL exists.

    The check itself is deterministic: ``seed_gaps`` lowers the requirement wording onto
    the schema exactly as step 05 will, and reports the declared values no entity stores.
    Only the schema update needs the model. A gap that survives the repair attempts is
    raised here, so a prerequisite without storage cannot reach step 05 and be discovered
    after the tables were already written.
    """
    tree = load_tree(arc_dir)
    prerequisites = seed_prerequisites(tree)
    gaps = seed_gaps(tree, schema)
    record = {"ok": not gaps, "prerequisites": prerequisites, "gaps": gaps, "attempts": []}
    if not gaps:
        return schema
    owners = sorted({item["source"].split("/")[0] for item in prerequisites})
    for attempt in range(1, attempts + 1):
        console.show(f"Seed storability repair {attempt}/{attempts} | Prerequisites without storage",
            "\n".join(gaps))
        request = {
            "seed_prerequisites": prerequisites,
            "missing_storage": gaps,
            "symbol_inventory": {name: list(entity.get("fields") or {}) for name, entity in schema["entities"].items()},
            "retrieved_schema": {"entities": schema["entities"]},
        }
        def accept(patch):
            updated = apply_patch(schema, patch)
            related = sorted(set(patch.get("related_fields") or []))
            for rid in owners:
                if rid in updated["requirements"]:
                    updated["requirements"][rid] = sorted(set(updated["requirements"][rid]) | set(related))
            return updated, format_changes(schema, updated)

        (updated, changes), _ = propose_json(model, SEED_INSTRUCTIONS, request, accept,
            console, f"Seed storability repair {attempt}/{attempts}", max_attempts)
        schema = updated
        save_json(arc_dir / "db_schema.json", schema)
        record["attempts"].append({"attempt": attempt, "changes": changes})
        gaps = seed_gaps(tree, schema)
        if not gaps:
            break
    record["gaps"] = gaps
    record["ok"] = not gaps
    save_json(arc_dir / "seed_precheck.json", record)
    if gaps:
        raise SeedRejected("The schema cannot store the prerequisites the requirements declare", gaps)
    return schema


def build_db_schema(arc_dir: Path, pause: bool = False, top_k: int = 6, max_attempts: int = 10) -> dict:
    requirements_path = arc_dir / "requirements.json"
    if not requirements_path.is_file():
        raise ValueError("Run 01_explore_requirement.py first to create .arc/requirements.json")
    requirements = json.loads(requirements_path.read_text(encoding="utf-8-sig"))
    # --- Extension point 02B: traversal and analysis context -------------------
    # RequirementTree owns traversal and step ownership. Study how another visit
    # order or richer node context changes the schema available to later nodes.
    nodes = list(requirement_nodes(requirements))
    if top_k < 1:
        raise ValueError("--top-k must be positive")
    console = DemoConsole(pause)
    retriever = SchemaRetriever()
    model = TextModel()
    schema = {"entities": {}, "requirements": {}}
    for index, requirement in enumerate(nodes, 1):
        label = f"{index}/{len(nodes)} {requirement['id']}"
        console.show(f"{label} | 1. Current requirement", (
            f"{requirement['name']}\n\nDescription:\n{requirement['description']}"
            f"\n\nConcatenated steps:\n{requirement['steps'] or '(No steps on this requirement.)'}"
        ))
        console.show(f"{label} | 2. All existing schema (excerpt)", format_schema(schema["entities"], excerpt=True))

        # --- Key extension point 02C: retrieval / RAG --------------------------
        # Rewrite query construction here and ranking/candidate selection in
        # infrastructure/shared/rag/schema.py: BM25, embeddings, hybrid search,
        # or reranking are possible experiments. Return {matches, entities};
        # retrieved entity definitions must retain their actual schema names.
        query = "\n".join(requirement[key] for key in ("name", "description", "steps"))
        retrieved = retriever.retrieve(query, schema, top_k)
        ranking = "\n".join(f"{item['symbol']}  BM25={item['score']:.4f}" for item in retrieved["matches"])
        console.show(f"{label} | 3. Retrieved schema", (
            (ranking or "No lexical matches (the schema may still be empty).")
            + "\n\nRetrieved entity definitions:\n"
            + format_schema(retrieved["entities"])
        ))

        # --- Extension point 02D: generation and JSON parse feedback -----------
        # Change the evidence supplied to the model or its correction strategy.
        # The name inventory is global; full definitions come from retrieval.
        # apply_patch() merges the proposed entity and field updates.
        request = {
            "requirement": requirement,
            "symbol_inventory": {name: list(entity["fields"]) for name, entity in schema["entities"].items()},
            "retrieved_schema": {"entities": retrieved["entities"]},
        }
        def accept(patch):
            updated = apply_patch(schema, patch)
            updated["requirements"][requirement["id"]] = sorted(set(patch["related_fields"]))
            changes = format_changes(schema, updated)
            return updated, changes

        (updated, changes), patch = propose_json(model, INSTRUCTIONS, request, accept,
            console, f"Schema design {requirement['id']}", max_attempts)
        # --- Pass boundary: publish the schema ----------------------
        # Subsequent requirements retrieve from this updated graph.
        save_json(arc_dir / "db_schema.json", updated)
        console.show(f"{label} | 4. Schema changes (saved)", (
            changes
        ))
        schema = updated
        console.artifact("db_schema", schema, f"{label} | Accepted schema")
    # --- Pass boundary: the schema must be able to store the declared prerequisites ---
    # Step 05 derives its seed rows from these same sentences, so proving storability here
    # keeps a missing entity from surfacing only after the DDL was already written.
    schema = repair_seed_storability(arc_dir, schema, model, console, max_attempts)
    save_json(arc_dir / "db_schema.json", schema)
    print(f"Schema saved: {arc_dir / 'db_schema.json'}", flush=True)
    return schema


def main() -> None:
    parser = create_parser("Step 02: Build DB Schema with RAG")
    parser.add_argument("--top-k", type=int, default=6, help="Number of BM25 entity/field hits (default: 6)")
    parser.add_argument("--max-attempts", type=int, default=10, help="Maximum proposals per requirement; including the first proposal (default: 10)")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            _, output_dir = resolve_directories(args)
            stack.enter_context(console_log(output_dir, debug_model_io=args.debug_model_io))
            build_db_schema(output_dir / ".arc", args.pause, args.top_k, args.max_attempts)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")


if __name__ == "__main__":
    main()

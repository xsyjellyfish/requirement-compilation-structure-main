"""Step 05: implement one SQLite CREATE TABLE file per schema entity."""

import json
from contextlib import ExitStack
from pathlib import Path

from infrastructure.utils.cli import create_parser, resolve_directories
from infrastructure.stages.database.acceptance import (
    TABLES_ROOT, SchemaRejected, published_tables, record_problems, smoke_problems)
from infrastructure.stages.database.initial_data import (
    SEED_ROOT, SeedRejected, initial_data, load_tree, seed_data_source, seed_problems)
from infrastructure.stages.database.order import implementation_groups, table_dependencies
from infrastructure.stages.database.runtime import initializer_source
from infrastructure.shared.models.text import TextModel
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.utils.presentation.formatting import format_schema
from infrastructure.utils.presentation.logging import console_log
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.utils.files import save_json, save_text


# --- Extension point 05A: SQL lowering policy ---------------------------------
# The model writes DDL; Python publishes it at the supplied table path.
# Executable behavior is exercised by later application tests.
INSTRUCTIONS = """Implement entity as SQLite DDL for the compiler-selected output_path.
Return {"content": "complete SQL file contents"}. The compiler writes output_path.
The content string contains SQL text without Markdown fences.

Create the current entity with CREATE TABLE IF NOT EXISTS, preserving its entity and
field names and quoting identifiers with double quotes. Implement the supplied keys,
nullability, uniqueness, references and SQLite-enforceable value constraints.
Map string/uuid/date/datetime/json to TEXT, integer/boolean to INTEGER, number to REAL.
Use explicit NOT NULL when nullable=false. SQLite INTEGER PRIMARY KEY can generate
its value; application code supplies other generated values unless schema evidence
specifies otherwise. Do not invent defaults, normalization, cascades or uniqueness.

Use CHECK for enforceable constraints such as boolean 0/1 or enumerations. Describe
application-only rules such as password hashing in comments instead of inventing SQL
checks. This task creates one table, without extra tables, data mutations or application startup code.
referenced_tables provides target definitions and available SQL. A null implementation
means the target has not been generated yet; SQLite forward references can express
self-references and cycles. The initializer enables foreign keys and runs the ordered
SQL files in a transaction. Preserve existing data through IF NOT EXISTS.
"""


def implement_db(arc_dir: Path, pause: bool = False, max_attempts: int = 10, repair: dict | None = None) -> dict:
    """Implement one SQLite table per entity and accept only SQL that stores a legal record.

    ``repair`` is the Step 05 request a later pass produced: only the named tables are
    rewritten with that feedback, while the SQL a previous run published is kept, so a
    retry changes exactly what failed instead of the whole database.
    """
    source = arc_dir / "db_schema.json"
    if not source.is_file():
        raise ValueError("Run 02_build_db_schema.py first to create .arc/db_schema.json")
    output_dir = arc_dir.parent.resolve()
    for required in ("backend/package.json", "backend/src/db/client.ts", "backend/src/db/initialize.ts"):
        if not (output_dir / required).is_file():
            raise ValueError("Run 00_project_initialization.py first to initialize the project structure")
    schema = json.loads(source.read_text(encoding="utf-8-sig"))
    console = DemoConsole(pause)
    # --- Extension point 05B: dependency-aware table implementation -----------
    # Referenced tables precede dependents. Cyclic groups use forward references;
    # available implementations and unresolved target definitions remain explicit.
    groups = implementation_groups(schema)
    order = [name for group in groups for name in group]
    dependencies = table_dependencies(schema)
    cycles = {name: group for group in groups for name in group}
    requested = {name: feedback for name, feedback in (repair or {}).items() if name in schema["entities"]}
    todo = [name for name in order if not requested or name in requested]
    print(("Table repair order: " if requested else "Table implementation order: ")
        + (" -> ".join(todo) or "(No tables.)"), flush=True)
    model = TextModel() if todo else None
    implementations = {name: {"path": f"{TABLES_ROOT}/{name}.sql", "content": content}
        for name, content in published_tables(output_dir, order).items()}
    sql = {name: item["content"] for name, item in implementations.items()}

    def implement(name: str, label: str, correction: dict | None = None) -> None:
        """Publish one table only after SQLite executes it and stores a legal record."""
        entity = schema["entities"][name]
        cycle = len(cycles[name]) > 1 or name in dependencies[name]
        console.show(f"{label} | 1. Table and dependency plan", (
            "Order: " + " -> ".join(order)
            + "\nReferences: " + (", ".join(dependencies[name]) or "(none)")
            + ("\nCyclic group (SQLite forward references): " + ", ".join(cycles[name]) if cycle else "")
            + "\n\n" + format_schema({name: entity})
        ))
        referenced = {
            target: {"schema": schema["entities"][target], "implementation": implementations.get(target)}
            for target in dependencies[name]
        }
        console.show(f"{label} | 2. Referenced table implementations", (
            "\n\n".join(
                f"{target}:\n" + (item["implementation"]["path"] + "\n" + item["implementation"]["content"]
                if item["implementation"] else "Not implemented yet; target schema is supplied for the forward reference.\n" + format_schema({target: item["schema"]}))
                for target, item in referenced.items()
            ) or "(No referenced tables.)"
        ))
        # --- Extension point 05C: generation context and correction -------
        expected_path = f"{TABLES_ROOT}/{name}.sql"
        request = {
            "entity_name": name, "entity": entity, "output_path": expected_path,
            "referenced_tables": referenced,
        }
        if correction:
            request["correction"] = correction

        def accept(response):
            """Reject SQL that SQLite cannot execute for a legal record of this entity."""
            content = str(response["content"]).rstrip() + "\n"
            found = record_problems(name, content, sql, schema, order)
            if found:
                raise SchemaRejected(name, found)
            return content

        content, _ = propose_json(model, INSTRUCTIONS, request, accept, console,
            f"SQL implementation {name}", max_attempts)
        proposal = {"path": expected_path, "content": content}
        console.show(f"{label} | 3. Model file proposal", expected_path + "\n\n" + content)
        target = output_dir / expected_path
        if not target.resolve().is_relative_to(output_dir):
            raise ValueError(f"Implementation path escapes the output directory: {expected_path}")
        save_text(target, content)
        implementations[name] = proposal
        sql[name] = content
        console.show(f"{label} | 4. Table implementation saved", (
            f"{target}\n\nSQL proposal saved and executed: SQLite stored a legal {name} record."
            + "\nValue CHECK constraints and application-only rules remain available for semantic review."
        ))

    for position, name in enumerate(todo, 1):
        implement(name, f"{position}/{len(todo)} {name}", requested.get(name))
    # --- Pass boundary: activate only a table set SQLite can write a row into ---
    # The set is executed as a whole, so foreign keys are verified between tables that are
    # all supposed to exist. A defect that survives the per-table check is repaired here,
    # in the only pass that may rewrite the DDL, instead of by a pass that can only read it.
    failures = smoke_problems(sql, schema, order)
    for attempt in (1, 2):
        if not failures:
            break
        print("Database acceptance failed for " + ", ".join(sorted(failures))
            + f": requesting corrected SQL (repair {attempt}/2).", flush=True)
        for name in sorted(failures):
            implement(name, f"{name} | SQLite acceptance repair {attempt}/2",
                {"error": "SQLite cannot store a legal record in the declared table.",
                    "details": {"table": name, "problems": failures[name]}})
        failures = smoke_problems(sql, schema, order)
    if failures:
        raise SchemaRejected(", ".join(sorted(failures)),
            [problem for name in sorted(failures) for problem in failures[name]])
    # The executable loader is the order registry; no extra manifest is retained.
    initializer = output_dir / "backend/src/db/initialize.ts"
    if not initializer.resolve().is_relative_to(output_dir):
        raise ValueError("DB initializer path escapes the output directory")
    save_text(initializer, initializer_source(implementations))
    print(f"Database initialization wired: {initializer}", flush=True)
    # --- Pass boundary: publish the prerequisite records the requirements declare ---
    # A scenario that needs existing data states it in the requirement tree. The seed is
    # derived from those sentences, executed against the DDL just published, and written as
    # the module the runtime seeds from, so the deployed database and the compiled tests
    # share one source of prerequisite data. A prerequisite that cannot be stored stops the
    # pass here instead of leaving an application that fails its own scenarios.
    seeds = initial_data(load_tree(arc_dir), schema)
    save_json(arc_dir / "initial_data.json", seeds)
    seed_failures = seeds["problems"] + seed_problems(schema, sql, seeds, order)
    if seed_failures:
        raise SeedRejected("The prerequisites the requirements declare cannot be stored",
            seed_failures)
    seed_file = output_dir / SEED_ROOT
    if not seed_file.resolve().is_relative_to(output_dir):
        raise ValueError("Seed contract path escapes the output directory")
    save_text(seed_file, seed_data_source(seeds))
    print(f"Prerequisite records published: {len(seeds['rows'])} row(s) in {seed_file}"
        + (f"; {len(seeds['external'])} external prerequisite(s) recorded by contract"
            if seeds["external"] else ""), flush=True)
    return implementations


def main() -> None:
    parser = create_parser("Step 05: Implement DB Tables")
    parser.add_argument("--max-attempts", type=int, default=10, help="Maximum proposals per table; including the first proposal (default: 10)")
    args = parser.parse_args()
    with ExitStack() as stack:
        try:
            _, output_dir = resolve_directories(args)
            stack.enter_context(console_log(output_dir, debug_model_io=args.debug_model_io))
            implement_db(output_dir / ".arc", args.pause, args.max_attempts)
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(1, f"Execution failed: {error}\n")


if __name__ == "__main__":
    main()

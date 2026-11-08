import type Database from "better-sqlite3";
import { seedRows } from "./seed-data.js";

// Compiler-owned prerequisite seeding. The records are the "Seed prerequisites" the
// requirement tree declares, so the deployed database and the compiled tests start from
// the same data. Seeding is idempotent: every statement is INSERT OR IGNORE against the
// record's own identity columns, so a restart or a second fixture cannot duplicate a row.

export interface SeedResult {
  readonly applied: number;
  readonly present: number;
  readonly missing: readonly string[];
}

export function seedDatabase(connection: Database.Database): SeedResult {
  const missing: string[] = [];
  connection.transaction(() => {
    for (const row of seedRows) {
      connection.prepare(row.insert.sql).run(...row.insert.params);
    }
    for (const row of seedRows) {
      if (connection.prepare(row.verify.sql).all(...row.verify.params).length === 0) {
        missing.push(`${row.kind} [${row.table}: ${row.sources.join(", ")}]`);
      }
    }
  })();
  return { applied: seedRows.length, present: seedRows.length - missing.length, missing };
}

export function requireSeedData(connection: Database.Database): SeedResult {
  const result = seedDatabase(connection);
  if (result.missing.length) {
    throw new Error(
      "Initialization left declared prerequisite data missing: " + result.missing.join("; ")
      + ". The requirement states this data must exist, so the application must not start without it.",
    );
  }
  return result;
}

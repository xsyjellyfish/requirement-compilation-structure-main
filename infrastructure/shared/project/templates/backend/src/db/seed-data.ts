// Compiler-owned prerequisite records; step 05 regenerates this file from the
// "Seed prerequisites" statements of the requirement tree. Nothing here is model output.
export interface SeedStatement {
  readonly sql: string;
  readonly params: ReadonlyArray<string | number | null>;
}

export interface SeedMapping {
  readonly declared: string;
  readonly column: string;
  readonly how: "field" | "synonym" | "plural" | "transform";
  readonly reason: string;
}

export interface SeedRow {
  readonly kind: string;
  readonly table: string;
  readonly identity: Readonly<Record<string, string | number | null>>;
  readonly columns: Readonly<Record<string, string | number | null>>;
  readonly insert: SeedStatement;
  readonly verify: SeedStatement;
  readonly derived: ReadonlyArray<{ readonly column: string; readonly reason: string }>;
  readonly mapped: ReadonlyArray<SeedMapping>;
  readonly sources: ReadonlyArray<string>;
}

export const seedRows: readonly SeedRow[] = [];

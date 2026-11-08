"""Compiler-owned TypeScript HTTP harness used by the step 08 integration suites.

Source strings below are compiler-owned templates, not model output: step 08 renders
them next to the generated Express app so the backend test suite can exercise real
HTTP routing over an isolated in-memory database.
"""

SUPPORT_HEADER = r'''// Compiler-owned HTTP integration harness; step 08 regenerates this file.
// It boots the real Express application against an isolated in-memory SQLite database,
// so every assertion below observes the same code path a browser would use.
import assert from "node:assert/strict";
import { once } from "node:events";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import type { TestContext } from "node:test";
import { test } from "node:test";
import type Database from "better-sqlite3";
import { createApp } from "../../src/app.js";
import { createDatabaseClient } from "../../src/db/client.js";
import { initializeDatabase } from "../../src/db/initialize.js";
import { requireSeedData } from "../../src/db/seed.js";

export type Row = Record<string, unknown>;
export type Snapshot = Record<string, Row[]>;
export type Expectation = "registered" | "unknownRoute" | "documentedError" | "rejectionWithoutWrite";

export interface HttpResult {
  status: number;
  headers: Record<string, string>;
  text: string;
  body: unknown;
  contentType: string;
}

export interface RequestOptions {
  body?: unknown;
  headers?: Record<string, string>;
}

export class HttpTestServer {
  private readonly http: Server;
  private readonly connection: Database.Database;
  readonly baseUrl: string;

  constructor(http: Server, connection: Database.Database, baseUrl: string) {
    this.http = http;
    this.connection = connection;
    this.baseUrl = baseUrl;
  }

  async request(method: string, path: string, options: RequestOptions = {}): Promise<HttpResult> {
    const headers: Record<string, string> = { accept: "application/json", ...(options.headers ?? {}) };
    let payload: string | undefined;
    if (options.body !== undefined) {
      headers["content-type"] = "application/json";
      payload = JSON.stringify(options.body);
    }
    const response = await fetch(this.baseUrl + path, { method, headers, body: payload });
    const text = await response.text();
    let body: unknown = text;
    if (text) {
      try { body = JSON.parse(text); } catch { body = text; }
    }
    return { status: response.status, headers: Object.fromEntries(response.headers.entries()),
      text, body, contentType: response.headers.get("content-type") ?? "" };
  }

  snapshot(): Snapshot {
    const tables = this.connection.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").all() as Array<{ name: string }>;
    const snapshot: Snapshot = {};
    for (const { name } of tables) {
      snapshot[name] = this.connection.prepare("SELECT * FROM \"" + name.replaceAll("\"", "\"\"") + "\"").all() as Row[];
    }
    return snapshot;
  }

  async close(): Promise<void> {
    this.http.closeAllConnections();
    await new Promise<void>((resolve, reject) => {
      this.http.close(error => (error ? reject(error) : resolve()));
    });
    this.connection.close();
  }
}

export async function createTestServer(context: TestContext): Promise<HttpTestServer> {
  // ":memory:" is a private database per test: no file, no shared state, no production rows.
  const connection = createDatabaseClient(":memory:");
  initializeDatabase(connection);
  // The requirement declares prerequisite data; the suite starts from it instead of
  // inventing a different state, and a missing record fails the setup loudly.
  requireSeedData(connection);
  const http = createApp(connection).listen(0, "127.0.0.1");
  await Promise.race([once(http, "listening"), once(http, "error").then(([error]) => { throw error; })]);
  const address = http.address() as AddressInfo;
  const server = new HttpTestServer(http, connection, "http://127.0.0.1:" + String(address.port));
  context.after(async () => { await server.close(); });
  return server;
}
'''

SUPPORT_PROBES = r'''
export function describeResult(result: HttpResult): string {
  const body = result.text.length > 240 ? result.text.slice(0, 240) + "..." : result.text;
  return "HTTP " + String(result.status) + " " + body;
}

export function isUnknownRoute(result: HttpResult): boolean {
  return result.status === 404 && /unknown api route/i.test(result.text);
}

export function normalizeName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

export function addedRows(before: Snapshot, after: Snapshot): Array<{ table: string; row: Row }> {
  const added: Array<{ table: string; row: Row }> = [];
  for (const [table, rows] of Object.entries(after)) {
    const known = new Set((before[table] ?? []).map(row => JSON.stringify(row)));
    for (const row of rows) {
      if (!known.has(JSON.stringify(row))) added.push({ table, row });
    }
  }
  return added;
}

export function changedTables(before: Snapshot, after: Snapshot): string[] {
  return Object.keys(after).filter(table => JSON.stringify(before[table] ?? []) !== JSON.stringify(after[table] ?? []));
}

export interface ProbeCase {
  api: string;
  kind: string;
  expect: Expectation;
  title: string;
  method: string;
  path: string;
  body: unknown;
  statuses: number[];
  success: number;
}

export function contractTests(cases: ProbeCase[]): void {
  for (const probe of cases) {
    test(probe.api + " | " + probe.title, async (context) => {
      const server = await createTestServer(context);
      const before = server.snapshot();
      const response = await server.request(probe.method, probe.path,
        probe.body === null ? {} : { body: probe.body });
      const after = server.snapshot();
      const label = probe.method + " " + probe.path;
      if (probe.expect === "unknownRoute") {
        assert.ok(isUnknownRoute(response),
          label + " must stay unrouted for " + probe.api + ", but the request reached a handler ("
            + describeResult(response) + "). Register only the methods declared by the API contract.");
        return;
      }
      assert.ok(!isUnknownRoute(response),
        label + " never reached an Express route (" + describeResult(response) + "). " + probe.api
          + " is missing from registerApis, or its method, path or path-parameter name differs from the contract.");
      if (response.status !== 204 && response.status !== 304) {
        assert.match(response.contentType, /application\/json/i,
          label + " must answer JSON, but sent " + (response.contentType || "no content type")
            + " (" + describeResult(response) + ").");
      }
      if (probe.expect === "registered") return;
      assert.ok(probe.statuses.includes(response.status),
        label + " answered " + String(response.status) + ", which the API contract does not document. Expected one of "
          + probe.statuses.join(", ") + " (" + describeResult(response) + ").");
      assert.notEqual(response.status, probe.success,
        label + " answered the documented success status " + String(probe.success)
          + " for a request that must be rejected (" + describeResult(response) + ").");
      assert.deepEqual(after, before,
        label + " wrote to the database (" + (changedTables(before, after).join(", ") || "no table changed")
          + ") while rejecting the request. A rejected operation must not create or modify records.");
    });
  }
}
'''

SUPPORT_FLOWS = r'''
export interface CrudStep {
  kind: "create" | "list" | "read" | "update" | "delete" | "read-empty";
  api: string;
  method: string;
  path: string;
  body: unknown;
  success: number;
  statuses: number[];
  contains: string | null;
  scope: string | null;
  checkBody: boolean;
  echo: string | null;
  required: boolean;
}

export interface CrudFlow {
  resource: string;
  steps: CrudStep[];
}

export function crudFlowTests(flows: CrudFlow[]): void {
  for (const flow of flows) {
    test("CRUD flow | " + flow.resource, async (context) => { await runFlow(context, flow); });
  }
}

async function runFlow(context: TestContext, flow: CrudFlow): Promise<void> {
  const server = await createTestServer(context);
  const values: Record<string, string> = {};
  // Steps the flow needs to succeed but could not: the journey is then UNVERIFIED, never PASS.
  const unverified: string[] = [];
  let verified = 0;
  let skipped = "";

  function fill(text: string, encode: boolean): string | undefined {
    let missing = "";
    const rendered = text.replace(/\{\{([^}]+)\}\}/g, (_match, key: string) => {
      const value = values[key];
      if (value === undefined) { missing = key; return ""; }
      return encode ? encodeURIComponent(value) : value;
    });
    if (missing) {
      skipped = "no created value is available for " + missing;
      return undefined;
    }
    return rendered;
  }

  function render(path: string): string | undefined {
    return fill(path, true);
  }

  // A borrowed value travels in the request body too: a sign-in submits the username and
  // password the registration flow already used, so both places resolve the same way.
  function renderBody(value: unknown): unknown {
    if (typeof value === "string") return value.includes("{{") ? fill(value, false) : value;
    if (Array.isArray(value)) return value.map(renderBody);
    if (value !== null && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, renderBody(item)]));
    }
    return value;
  }

  for (const step of flow.steps) {
    if (skipped) { context.diagnostic("skipped " + step.api + ": " + skipped); continue; }
    const path = render(step.path);
    if (path === undefined) continue;
    const body = step.body === null ? null : renderBody(step.body);
    if (skipped) continue;
    const label = step.api + " | " + step.method + " " + path;
    const before = server.snapshot();
    const response = await server.request(step.method, path, body === null ? {} : { body });
    const after = server.snapshot();
    assert.ok(!isUnknownRoute(response),
      label + " never reached an Express route (" + describeResult(response) + ")."
        + " The route is not registered, or its method, path or path-parameter name differs from the contract.");
    if (step.kind === "create") {
      if (response.status !== step.success) {
        if (!step.statuses.includes(response.status)) {
          assert.fail(label + " answered " + String(response.status) + ", which the API contract does not document. Expected "
            + String(step.success) + " or " + step.statuses.join(", ") + " (" + describeResult(response) + ").");
        }
        skipped = label + " answered " + String(response.status)
          + "; this example payload needs fields the contract cannot derive (" + describeResult(response) + ")";
        context.diagnostic(skipped);
        continue;
      }
      const added = addedRows(before, after);
      assert.ok(added.length > 0,
        label + " answered " + String(response.status) + " but persisted no row ("
          + (changedTables(before, after).join(", ") || "no table changed") + ").");
      if (step.echo) {
        assert.ok(response.text.toLowerCase().includes(step.echo.toLowerCase()),
          label + " answered " + String(response.status) + " without returning the submitted value "
            + JSON.stringify(step.echo) + " (" + describeResult(response)
            + "). A created record must be readable from its own response.");
      }
      for (const { row } of added) {
        for (const [column, value] of Object.entries(row)) {
          if (typeof value === "string" || typeof value === "number" || typeof value === "bigint") {
            values[(step.scope ?? step.path) + "::" + normalizeName(column)] = String(value);
          }
        }
      }
      // A later step reuses what this request submitted: the row only holds what the handler
      // chose to persist, so a secret such as the password never appears as a column. The
      // stored row stays authoritative for every column it does publish.
      const submitted = (body ?? {}) as Record<string, unknown>;
      for (const [name, value] of Object.entries(submitted)) {
        const key = (step.scope ?? step.path) + "::" + normalizeName(name);
        if (values[key] === undefined
            && (typeof value === "string" || typeof value === "number" || typeof value === "bigint")) {
          values[key] = String(value);
        }
      }
      verified += 1;
      continue;
    }
    if (step.kind === "read-empty") {
      assert.notEqual(response.status, step.success,
        label + " still answers the success status " + String(step.success)
          + " after the resource was deleted (" + describeResult(response) + ").");
      continue;
    }
    if (response.status === step.success) {
      if ((step.kind === "read" || step.kind === "list") && step.checkBody && step.contains) {
        assert.ok(response.text.toLowerCase().includes(step.contains.toLowerCase()),
          label + " answered " + String(response.status) + " without the created value \"" + step.contains + "\" ("
            + describeResult(response) + "). The path parameter is not reaching the handler, or the read does not return the stored record.");
      }
      if (step.kind === "update") {
        assert.ok(changedTables(before, after).length > 0,
          label + " answered " + String(response.status) + " without changing the database.");
        if (step.contains) {
          assert.ok(JSON.stringify(after).toLowerCase().includes(step.contains.toLowerCase()),
            label + " answered " + String(response.status) + " but no stored row contains the updated value \""
              + step.contains + "\".");
        }
      }
      if (step.kind === "delete" && step.contains) {
        assert.ok(!JSON.stringify(after).toLowerCase().includes(step.contains.toLowerCase()),
          label + " answered " + String(response.status) + " but the deleted record is still stored (found \""
            + step.contains + "\").");
      }
      verified += 1;
      continue;
    }
    if (step.statuses.includes(response.status)) {
      if (response.status === 404) {
        assert.fail(label + " answered 404 for the record created moments earlier (" + describeResult(response)
          + "). The route or its path parameter is not reaching the handler.");
      }
      // The contract allows this rejection, but the flow needs the documented success: only
      // the error path was exercised, so the journey stops and stays UNVERIFIED, never PASS.
      unverified.push(label + " answered the documented rejection " + String(response.status)
        + " where the flow needs " + String(step.success) + " (" + describeResult(response) + ")");
      break;
    }
    assert.fail(label + " answered " + String(response.status) + ", which the API contract does not document. Expected "
      + String(step.success) + " or " + step.statuses.join(", ") + " (" + describeResult(response) + ").");
  }
  if (skipped) unverified.push(skipped);
  if (unverified.length > 0) {
    assert.fail("CRUD flow " + flow.resource + " was not verified: " + unverified.join("; ")
      + ". Only a flow whose documented success was actually observed counts as passing; a suite that accepts"
      + " rejections instead proves the error path and nothing else.");
  }
  assert.ok(verified > 0,
    "CRUD flow " + flow.resource + " executed no successful step, so the journey is unproven.");
}
'''


def http_support_source() -> str:
    """Render the compiler-owned TypeScript harness shared by every HTTP suite."""
    return "\n".join((SUPPORT_HEADER, SUPPORT_PROBES, SUPPORT_FLOWS))



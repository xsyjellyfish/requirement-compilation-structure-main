"""Compiler-owned Playwright harness for the black-box acceptance layer.

Nothing in this module is model output. The generated application gets a second,
independent Playwright project (own config, own fixture, own runner, own temporary
database) whose only purpose is to reproduce what an external reviewer does:

* a real Chromium browser driven through the UI, without mocks or app hooks;
* every test in its own browser context, so no test can inherit another test's cookies,
  storage or in-memory state, and no test depends on execution order;
* semantic locators (role, accessible name, label, placeholder) with ``data-testid`` only
  as a fallback;
* the SQLite database is exposed read-only and only for side-effect assertions, so a
  Given state can never be fabricated by the test;
* the compiler's own API client still reports contract violations, so a request the
  contract does not declare fails the acceptance run as well.
"""

import json

from infrastructure.stages.frontend.ui.runtime import PLAYWRIGHT_VERSION
from infrastructure.utils.files import save_text

CONFIG = "frontend/playwright.acceptance.config.ts"
SUPPORT = "frontend/tests/acceptance/support/context.ts"
RUNNER = "frontend/scripts/test-acceptance.mjs"
VITE_CONFIG = "frontend/vite.acceptance.config.ts"
SCRIPT = "test:acceptance"
TEMP_PREFIX = "arc-acceptance-"

CONFIG_SOURCE = '''import { defineConfig } from "@playwright/test";
const frontendPort = process.env.ARC_ACCEPTANCE_FRONTEND_PORT;
const backendPort = process.env.ARC_ACCEPTANCE_BACKEND_PORT;
const database = process.env.ARC_ACCEPTANCE_DB_FILE;
if (!frontendPort || !backendPort || !database) throw new Error("Use npm run test:acceptance or the step 12/13 runner");
export default defineConfig({
  testDir: "./tests/acceptance", outputDir: "./test-results/acceptance", workers: 1,
  fullyParallel: false, retries: 0, forbidOnly: true,
  timeout: 90000, expect: { timeout: 15000 }, reporter: "json",
  // No storageState: every test receives a new, empty browser context.
  use: { baseURL: `http://127.0.0.1:${frontendPort}`, browserName: "chromium", headless: true,
    storageState: undefined, trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    { command: "node --import tsx ../backend/src/server.ts",
      url: `http://127.0.0.1:${backendPort}/api/health`, reuseExistingServer: false, timeout: 600000,
      ...(process.platform === "win32" ? {} : { gracefulShutdown: { signal: "SIGTERM" as const, timeout: 3000 } }),
      env: { PORT: backendPort, DATABASE_URL: database } },
    { command: `node ../node_modules/vite/bin/vite.js --config vite.acceptance.config.ts --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      url: `http://127.0.0.1:${frontendPort}`, reuseExistingServer: false, timeout: 600000,
      ...(process.platform === "win32" ? {} : { gracefulShutdown: { signal: "SIGTERM" as const, timeout: 3000 } }),
      env: { ARC_ACCEPTANCE_BACKEND_URL: `http://127.0.0.1:${backendPort}` } },
  ],
});
'''

RUNNER_SOURCE = '''// Every acceptance run owns a new database; no production or development database is touched.
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";
import { spawn, spawnSync } from "node:child_process";
const root = fileURLToPath(new URL("../../", import.meta.url));
async function freePort() {
  const server = createServer();
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return port;
}
const directory = mkdtempSync(join(process.env.ARC_ACCEPTANCE_TEMP_ROOT ?? tmpdir(), "arc-acceptance-"));
try {
  const backend = await freePort();
  let frontend = await freePort();
  while (frontend === backend) frontend = await freePort();
  const child = spawn(process.execPath, ["node_modules/@playwright/test/cli.js", "test",
    "--config", "frontend/playwright.acceptance.config.ts", ...process.argv.slice(2)], {
    cwd: root, stdio: "inherit", env: { ...process.env, ARC_ACCEPTANCE_BACKEND_PORT: String(backend),
      ARC_ACCEPTANCE_FRONTEND_PORT: String(frontend), ARC_ACCEPTANCE_DB_FILE: join(directory, "app.db") },
  });
  const forward = signal => {
    if (process.platform === "win32" && child.pid)
      spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
    else child.kill(signal);
  };
  process.on("SIGINT", forward); process.on("SIGTERM", forward);
  try {
    process.exitCode = await new Promise((resolve, reject) => { child.once("error", reject); child.once("close", code => resolve(code ?? 1)); });
  } finally { process.off("SIGINT", forward); process.off("SIGTERM", forward); }
} finally { rmSync(directory, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 }); }
'''

VITE_SOURCE = ('import { defineConfig, mergeConfig } from "vite";\n'
    'import base from "./vite.config";\n'
    'export default mergeConfig(base, defineConfig({ server: { proxy: '
    '{ "/api": process.env.ARC_ACCEPTANCE_BACKEND_URL! } } }));\n')

SUPPORT_SOURCE = '''// Compiler-owned black-box acceptance harness; the compiler regenerates this file.
// A test may only drive the real UI. The database is exposed read-only, request interception
// is rejected, and display fixtures cannot be injected, so no test can fabricate a result.
import { test as base, expect } from "@playwright/test";
import type { BrowserContext, Locator, Page, TestInfo } from "@playwright/test";
import type Database from "better-sqlite3";
import { basename, dirname } from "node:path";
import { createDatabaseClient } from "../../../../backend/src/db/client.js";
import { initializeDatabase } from "../../../../backend/src/db/initialize.js";

export interface ReadOnlyDatabase {
  /** Rows of one read-only query. Writes are rejected: Given data must come from the UI. */
  rows<T = Record<string, unknown>>(sql: string, ...params: unknown[]): T[];
  /** First column of the first row, or undefined. */
  value<T = unknown>(sql: string, ...params: unknown[]): T | undefined;
  /** Row count of one table, optionally filtered. */
  count(table: string, where?: string, params?: unknown[]): number;
  /** Rows stored in every table; a rejected submission must not change it. */
  total(): number;
  /** Rows across all tables that store the given text; 0 proves the record is gone. */
  stores(value: string): number;
}

export interface Visitor { context: BrowserContext; page: Page }

const MOCK_FORBIDDEN = "E2E_MOCK_FORBIDDEN: black-box acceptance drives the real application; "
  + "request interception, mocks and route fulfillment are not allowed.";
const READ_ONLY = "E2E_SEED_FORBIDDEN: black-box acceptance may only read the database. "
  + "Given state must be created through the UI, never with SQL.";

function quote(identifier: string): string {
  return "\\"" + identifier.replaceAll("\\"", "\\"\\"") + "\\"";
}

function all<T>(connection: Database.Database, sql: string, params: unknown[]): T[] {
  return (connection.prepare(sql).all as (...values: unknown[]) => T[])(...params);
}

function single<T>(connection: Database.Database, sql: string, params: unknown[]): T | undefined {
  const rows = all<Record<string, T>>(connection, sql, params);
  const first = rows[0];
  return first === undefined ? undefined : Object.values(first)[0];
}

function isRead(sql: string): boolean {
  return /^\\s*(?:select|with)\\b/i.test(sql);
}

// Playwright's own interception API is the usual way a suite fakes a backend. Removing it
// turns that mistake into an immediate, named failure instead of a green, meaningless test.
function forbidInterception(target: object): void {
  for (const name of ["route", "routeFromHAR", "unroute", "unrouteAll"]) {
    Object.defineProperty(target, name, { configurable: true, value: () => { throw new Error(MOCK_FORBIDDEN); } });
  }
}

export const test = base.extend<{ database: ReadOnlyDatabase; visitor: Visitor; applicationErrors: void }>({
  database: [async ({}, use) => {
    const path = process.env.ARC_ACCEPTANCE_DB_FILE;
    if (!path || basename(path) !== "app.db" || !basename(dirname(path)).startsWith("arc-acceptance-"))
      throw new Error("Black-box acceptance requires the runner-owned temporary database");
    const connection = createDatabaseClient(path);
    const tables = () => all<{ name: string }>(connection,
      "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'", []).map(row => row.name);
    try {
      initializeDatabase(connection);
      connection.pragma("foreign_keys = OFF");
      connection.transaction(() => { for (const name of tables()) connection.exec("DELETE FROM " + quote(name)); })();
      connection.pragma("foreign_keys = ON");
      const check = (sql: string) => {
        if (!isRead(sql)) throw new Error(READ_ONLY + " Rejected: " + sql.trim().slice(0, 120));
      };
      const read: ReadOnlyDatabase = {
        rows<T>(sql: string, ...params: unknown[]): T[] { check(sql); return all<T>(connection, sql, params); },
        value<T>(sql: string, ...params: unknown[]): T | undefined { check(sql); return single<T>(connection, sql, params); },
        count(table: string, where?: string, params: unknown[] = []): number {
          const filter = where ? " WHERE " + where : "";
          return Number(single<number>(connection, "SELECT COUNT(*) AS count FROM " + quote(table) + filter, params) ?? 0);
        },
        total(): number {
          return tables().reduce((sum, name) => sum + Number(single<number>(connection,
            "SELECT COUNT(*) AS count FROM " + quote(name), []) ?? 0), 0);
        },
        stores(value: string): number {
          let found = 0;
          for (const name of tables()) {
            const columns = all<{ name: string }>(connection, "PRAGMA table_info(" + quote(name) + ")", [])
              .map(row => row.name);
            for (const column of columns) {
              found += Number(single<number>(connection, "SELECT COUNT(*) AS count FROM " + quote(name)
                + " WHERE INSTR(CAST(" + quote(column) + " AS TEXT), ?) > 0", [value]) ?? 0);
            }
          }
          return found;
        },
      };
      await use(read);
    } finally { connection.close(); }
  }, { auto: true }],
  visitor: [async ({ browser, baseURL }, use) => {
    // A second, fully independent browser context: no cookies, storage or cache from the test.
    const context = await browser.newContext({ baseURL });
    const page = await context.newPage();
    try { await use({ context, page }); } finally { await context.close(); }
  }, { auto: false }],
  applicationErrors: [async ({ context, page }, use) => {
    forbidInterception(context);
    forbidInterception(page);
    await context.addInitScript(() => {
      Object.defineProperty(window, "__ARC_UI_FIXTURE__", { configurable: false,
        get: () => undefined,
        set: () => { throw new Error("E2E_FIXTURE_FORBIDDEN: black-box acceptance loads real data; "
          + "a display fixture may not replace the API response."); } });
    });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push("E2E_RUNTIME_ERROR: " + error.message));
    page.on("requestfailed", request => {
      if (new URL(request.url()).pathname.startsWith("/api/")) errors.push("E2E_NETWORK_ERROR: " + request.url());
    });
    await use();
    // The canonical client records every request that contradicts the API contract or reached
    // no route, so a handler that swallows the error cannot hide it from acceptance.
    const violations = await page.evaluate(
      () => (window as unknown as { __ARC_API_VIOLATIONS__?: string[] }).__ARC_API_VIOLATIONS__ ?? [],
    ).catch(() => [] as string[]);
    for (const item of violations) errors.push(item);
    if (errors.length) throw new Error(errors.join("\\n"));
  }, { auto: true }],
});

export { expect };

// Semantic locators. An accessible name is what a screen reader announces and what an
// external black-box reviewer types into a locator; data-testid is only a fallback.
export function control(page: Page, name: string): Locator {
  return page.getByLabel(name, { exact: true })
    .or(page.getByPlaceholder(name, { exact: true }))
    .or(page.getByRole("textbox", { name, exact: true }));
}

export function action(page: Page, name: string): Locator {
  return page.getByRole("button", { name, exact: true })
    .or(page.getByRole("link", { name, exact: true }));
}

export function record(page: Page, name: string): Locator {
  return page.getByRole("link", { name, exact: true })
    .or(page.getByRole("button", { name, exact: true }))
    .or(page.getByRole("heading", { name, exact: true }));
}

export function errorMessage(page: Page): Locator {
  return page.getByRole("alert").or(page.locator("[aria-invalid='true']"));
}

/**
 * Open a route a visitor could open. A click may have queued the application's own navigation,
 * which can interrupt an immediate goto; wait for that navigation and retry once if it went
 * somewhere else.
 */
export async function openRoute(page: Page, path: string): Promise<void> {
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      await page.goto(path);
      return;
    } catch (error) {
      await page.waitForURL((url: URL) => url.pathname === path, { timeout: 5000 })
        .catch(() => undefined);
      if (new URL(page.url()).pathname === path) return;
      if (attempt === 2) throw error;
    }
  }
}

/** A fresh context may legitimately be redirected (a signed-in application); then only annotate. */
export async function expectIndependentlyVisible(visitor: Visitor, path: string, name: string,
    info: TestInfo): Promise<boolean> {
  await visitor.page.goto(path);
  if (new URL(visitor.page.url()).pathname !== path) {
    info.annotations.push({ type: "independent-context",
      description: "a fresh browser context was redirected from " + path
        + "; cross-session visibility of the record is not asserted" });
    return false;
  }
  await expect(visitor.page.getByText(name).first()).toBeVisible();
  return true;
}

/** Symmetric check: the rejected record is invisible to an unrelated visitor. */
export async function expectIndependentlyMissing(visitor: Visitor, path: string, name: string,
    info: TestInfo): Promise<boolean> {
  await visitor.page.goto(path);
  if (new URL(visitor.page.url()).pathname !== path) {
    info.annotations.push({ type: "independent-context",
      description: "a fresh browser context was redirected from " + path
        + "; cross-session absence of the record is not asserted" });
    return false;
  }
  await expect(record(visitor.page, name)).toHaveCount(0);
  return true;
}
'''


def prepare_acceptance(output) -> dict:
    """Publish the compiler-owned acceptance project: config, fixture, runner and npm script."""
    for relative, source in {CONFIG: CONFIG_SOURCE, SUPPORT: SUPPORT_SOURCE,
            RUNNER: RUNNER_SOURCE, VITE_CONFIG: VITE_SOURCE}.items():
        save_text(output / relative, source)
    path = output / "package.json"
    package = json.loads(path.read_text(encoding="utf-8-sig"))
    package.setdefault("devDependencies", {}).setdefault("@playwright/test", PLAYWRIGHT_VERSION)
    package.setdefault("scripts", {})[SCRIPT] = "node frontend/scripts/test-acceptance.mjs"
    save_text(path, json.dumps(package, indent=2) + "\n")
    return {"config": CONFIG, "support": SUPPORT, "runner": RUNNER, "script": SCRIPT}

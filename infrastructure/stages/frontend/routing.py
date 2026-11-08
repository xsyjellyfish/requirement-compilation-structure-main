"""Compiler-owned browser routing shared by step 10 and step 12.

Step 10 (presentation) and step 12 (interaction) both regenerate ``frontend/src/App.tsx``.
Emitting both variants from one router module keeps dynamic page routes — matching,
link building, navigation state and refresh/back/forward recovery — identical in the two
generators. The router is pure TypeScript: no React, no browser storage, no business
knowledge, so it can be executed by deterministic tests.
"""

from urllib.parse import quote

from infrastructure.shared.parameters import PARAMETER, PARAMETER_PATTERN
from infrastructure.utils.files import save_text

ROUTER_PATH = "frontend/src/ui-router.ts"

ROUTER_RUNTIME = '''// Compiler-owned routing: URL <-> page identity, decoded parameters and navigation state.
import type { UiRouteParams } from "./ui-runtime";

export interface UiRouteDefinition { id: string; route: string }

export interface UiRouteView<T extends UiRouteDefinition = UiRouteDefinition> {
  page: T | null; pageId: string; route: string; path: string;
  params: UiRouteParams; key: string;
}

export interface UiLocation { path: string; navigation: number }

// One parameter syntax for API paths and page routes: /books/:bookName or /books/{bookName}.
const PARAMETER = /__UI_ROUTE_PARAMETER__/g;
const WHOLE_PARAMETER = new RegExp("^(?:" + PARAMETER.source + ")$");

export function normalizePath(path: string): string {
  let target = path.split("?")[0].split("#")[0];
  while (target.length > 1 && target.endsWith("/")) target = target.slice(0, -1);
  return target || "/";
}

export function currentPath(): string {
  return normalizePath(window.location.pathname);
}

export function routeParameters(route: string): string[] {
  const source = new RegExp(PARAMETER.source, "g");
  return Array.from(normalizePath(route).matchAll(source), match => match[1] || match[2] || "");
}

function parameterName(segment: string): string | null {
  const match = WHOLE_PARAMETER.exec(segment);
  return match ? match[1] || match[2] || null : null;
}

function decodeSegment(segment: string): string | null {
  try { return decodeURIComponent(segment); } catch { return null; }
}

// Static segments compare literally; a parameter segment accepts one path segment.
export function matchRoute(route: string, pathname: string): UiRouteParams | null {
  const expected = normalizePath(route).split("/");
  const actual = normalizePath(pathname).split("/");
  if (expected.length !== actual.length) return null;
  const params: UiRouteParams = {};
  for (let index = 0; index < expected.length; index += 1) {
    const name = parameterName(expected[index]);
    const segment = actual[index];
    if (name === null) {
      if (expected[index] !== segment) return null;
      continue;
    }
    const value = segment ? decodeSegment(segment) : null;
    if (!value) return null;
    params[name] = value;
  }
  return params;
}

export function buildRoute(route: string, params: Record<string, unknown> = {}): string {
  const missing: string[] = [];
  const source = new RegExp(PARAMETER.source, "g");
  const path = normalizePath(route).replace(source, (token: string, braced?: string, colon?: string) => {
    const name = braced || colon || "";
    const value = params[name];
    if (value == null || String(value) === "") { missing.push(name); return token; }
    return encodeURIComponent(String(value));
  });
  const absent = missing.filter((name, index) => missing.indexOf(name) === index);
  if (absent.length) throw new Error(`Missing route parameter: ${absent.join(", ")}`);
  if (path.includes("{") || path.includes("}") || path.split("/").some(segment => segment.startsWith(":")))
    throw new Error(`Unresolved route parameter in route: ${route}`);
  return path;
}

// A declared static route wins over a dynamic one; otherwise declaration order decides.
export function routeView<T extends UiRouteDefinition>(definitions: readonly T[], pathname: string): UiRouteView<T> {
  const path = normalizePath(pathname);
  let matched: { page: T; params: UiRouteParams } | null = null;
  for (const page of definitions) {
    const params = matchRoute(page.route, path);
    if (params === null) continue;
    if (Object.keys(params).length === 0) { matched = { page, params }; break; }
    if (matched === null) matched = { page, params };
  }
  return matched === null
    ? { page: null, pageId: "", route: "", path, params: {}, key: path }
    : { page: matched.page, pageId: matched.page.id, route: normalizePath(matched.page.route), path, params: matched.params, key: path };
}

export function initialLocation(): UiLocation {
  return { path: currentPath(), navigation: 0 };
}

export function pushed(location: UiLocation, path: string): UiLocation {
  return { path: normalizePath(path), navigation: location.navigation + 1 };
}

export function popped(location: UiLocation): UiLocation {
  return { path: currentPath(), navigation: location.navigation + 1 };
}

// Remounts a page component and re-runs its entry triggers when the route parameters change.
export function mountKey(location: UiLocation, view: UiRouteView): string {
  return `${location.navigation}:${view.key}`;
}
'''


def router_source() -> str:
    """Render the canonical router with the compiler-wide route parameter syntax."""
    return ROUTER_RUNTIME.replace("__UI_ROUTE_PARAMETER__", PARAMETER_PATTERN)


def sync_router(output) -> None:
    """Write the shared router so both frontend stages route identically."""
    save_text(output / ROUTER_PATH, router_source())


def sample_route(route: str, values: dict | None = None) -> str:
    """Render a visitable URL for a route template so browser tests can open dynamic pages."""
    supplied = values or {}

    def substitute(match) -> str:
        name = match.group(1) or match.group(2)
        return quote(str(supplied.get(name, "example")), safe="")

    return PARAMETER.sub(substitute, route) or "/"

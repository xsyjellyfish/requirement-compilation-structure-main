"""Step 12 lowering: bind each DOM trigger to its implementation.

Compiler-owned TypeScript strings provide UI state, canonical API calls, navigation,
and event registration. The model implements each trigger's business logic; the
compiler activates only the triggers accepted or currently being implemented."""

from hashlib import sha256
import json

from infrastructure.stages.frontend.ui.runtime import UI_RUNTIME
from infrastructure.stages.frontend.routing import sync_router
from infrastructure.stages.frontend.triggers.contracts import api_contract
from infrastructure.shared.api.paths import PARAMETER_PATTERN
from infrastructure.shared.project.entrypoints import import_path, implementation_entrypoint, selected_path, remember_path
from infrastructure.utils.files import save_text

TRIGGER_REGISTRY = "frontend/src/ui-triggers.ts"
API_CLIENT = "frontend/src/ui-api.ts"


def prepare_trigger_paths(output, identities):
    """Reserve writable targets so one edits list can implement a complete requirement."""
    paths = {}
    for tid in sorted(identities):
        relative = selected_path(output, "trigger", tid)
        if relative is None:
            relative = "frontend/src/triggers/trigger-" + sha256(tid.encode()).hexdigest()[:16] + ".ts"
            remember_path(output, "trigger", tid, relative)
        paths[tid] = relative
    return paths

CLIENT_SOURCE = '''
const ROUTE_PARAMETER = /__API_PATH_PARAMETER__/g;
const SAFE_HEADERS = ["content-type", "accept", "accept-language", "x-requested-with"];

export function apiPath(path: string, params: Record<string, unknown> = {}): string {
  const missing: string[] = [];
  const resolved = path.replace(ROUTE_PARAMETER, (token: string, braced?: string, colon?: string) => {
    const key = braced ?? colon ?? "";
    const value = params[key];
    if (value == null || value === "") {
      missing.push(key);
      return token;
    }
    return encodeURIComponent(String(value));
  });
  if (missing.length) throw new Error(`Missing route parameter: ${missing.filter((key, index) => missing.indexOf(key) === index).join(", ")}`);
  if (/[{}]/.test(resolved)) throw new Error(`Unresolved route parameter in path: ${path}`);
  return resolved;
}

function reported(message: string): void {
  if (typeof window === "undefined") return;
  const scope = window as unknown as { __ARC_API_VIOLATIONS__?: string[] };
  scope.__ARC_API_VIOLATIONS__ = [...(scope.__ARC_API_VIOLATIONS__ ?? []), message];
}

function supplied(values: Record<string, unknown> | undefined): string[] {
  return Object.entries(values ?? {})
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key]) => key);
}

/** Compare one request with the accepted contract before anything leaves the browser. */
export function apiRequestProblems(contract: ApiContract, request: UiApiRequest): string[] {
  if (!contract.strict) return [];
  const route = `${contract.method} ${contract.path}`;
  const problems: string[] = [];
  const params = supplied(request.params);
  for (const key of params) if (!contract.params.includes(key)) problems.push(`request.params.${key} is not a route parameter of ${route}`);
  for (const key of contract.params) if (!params.includes(key)) problems.push(`route parameter ${key} of ${contract.path} is missing from request.params`);
  const query = supplied(request.query);
  for (const key of query) if (!contract.query.includes(key)) problems.push(`request.query.${key} is not a declared query parameter of ${route}`);
  for (const key of contract.requiredQuery) if (!query.includes(key)) problems.push(`query parameter ${key} is required by ${route}`);
  const body = supplied(request.body);
  for (const key of body) if (!contract.body.includes(key)) problems.push(`request.body.${key} is not a declared body field of ${route}`);
  for (const key of contract.requiredBody) if (!body.includes(key)) problems.push(`body field ${key} is required by ${route}`);
  if ((contract.method === "GET" || contract.method === "HEAD") && body.length) problems.push(`${route} cannot carry request.body: a read sends no body`);
  const declared = contract.headers.map(name => name.toLowerCase());
  for (const key of supplied(request.headers)) {
    const name = key.toLowerCase();
    if (!SAFE_HEADERS.includes(name) && !declared.includes(name)) problems.push(`request.headers.${key} is not declared by ${route}`);
  }
  return problems.filter((item, index) => problems.indexOf(item) === index);
}

export async function callApi(apiId: string, request: UiApiRequest = {}): Promise<UiApiResponse> {
  const contract = catalog[apiId];
  if (!contract) throw new Error(`Unknown backend API: ${apiId}`);
  const problems = apiRequestProblems(contract, request);
  if (problems.length) {
    const message = `E2E_API_CONTRACT_ERROR: context.api(${JSON.stringify(apiId)}) contradicts ${contract.method} ${contract.path}: ${problems.join("; ")}`;
    reported(message);
    throw new Error(message);
  }
  const url = new URL(apiPath(contract.path, request.params ?? {}), window.location.origin);
  for (const [key, value] of Object.entries(request.query ?? {})) {
    if (value == null) continue;
    for (const item of Array.isArray(value) ? value : [value]) url.searchParams.append(key, String(item));
  }
  const headers = new Headers(request.headers);
  headers.delete("cookie"); headers.delete("set-cookie");
  const init: RequestInit = { method: contract.method, credentials: request.credentials ?? "same-origin", headers };
  if (contract.method !== "GET") {
    headers.set("content-type", "application/json");
    init.body = JSON.stringify(request.body ?? {});
  }
  const response = await fetch(url, init);
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (response.status === 404 && JSON.stringify(body ?? "").includes("Unknown API route"))
    reported(`E2E_API_ROUTE_ERROR: context.api(${JSON.stringify(apiId)}) sent ${contract.method} ${url.pathname} and the backend answered 404 Unknown API route`);
  return { status: response.status, body, headers: Object.fromEntries(response.headers.entries()) };
}
'''

APP_SOURCE = '''
export function App() {
  const [data, setData] = useState<Record<string, unknown>>(() => ({ ...defaults }));
  const live = useRef(data);
  const [location, setLocation] = useState(initialLocation);
  const entered = useRef("");
  const epoch = useRef(0);
  const view = routeView(pages, location.path);
  const pageId = view.pageId;
  const display = initialPageData(pageId, data);
  const fixture = display !== data;

  const update = (patch: Record<string, unknown>) => {
    live.current = { ...live.current, ...patch };
    setData(live.current);
  };
  const navigate = (id: string, params: UiRouteParams = {}) => {
    const page = pages.find(item => item.id === id);
    if (!page) throw new Error(`Unknown page: ${id}`);
    const target = buildRoute(page.route, params);
    epoch.current += 1;
    window.history.pushState({}, "", target);
    setLocation(current => pushed(current, target));
  };
  const invoke = async (trigger: Trigger, input: UiTriggerContext["input"]) => {
    const handler = handlers[trigger.id];
    if (!handler) return;
    let started = epoch.current;
    const context: UiTriggerContext = {
      get data() { return live.current; }, input,
      trigger: { id: trigger.id, source: trigger.source, event: trigger.event },
      get params() { return view.params; },
      get route() { return view.path; },
      update: patch => {
        if (started === epoch.current) update(patch);
      },
      navigate: (id, params) => {
        if (started === epoch.current) { navigate(id, params); started = epoch.current; }
      },
      api: (id, request) => {
        return callApi(id, request);
      },
    };
    await handler(context);
  };
  const report = (error: unknown) => { setTimeout(() => { throw error; }, 0); };
  const capture = (kind: string, event: SyntheticEvent) => {
    if (!fixture && (kind === "input" || kind === "change")) syncInput(event.target, inputBindings, update);
    if (kind === "submit") event.preventDefault();
    if (fixture) return;
    let target = event.target instanceof Element ? event.target : null;
    const sources: string[] = [];
    while (target && target !== event.currentTarget) {
      const id = target.getAttribute("data-testid");
      if (id) sources.push(id);
      target = target.parentElement;
    }
    const matches = sources.flatMap(source => triggers.filter(trigger => trigger.source === source && trigger.event === kind));
    if (matches.length && ["click", "submit", "keydown"].includes(kind)) event.preventDefault();
    const control = event.target as HTMLInputElement;
    const input = { value: control.value, checked: control.checked, key: (event as KeyboardEvent).key };
    void (async () => {
      for (const trigger of matches) {
        await invoke(trigger, input);
      }
    })().catch(report);
  };
  useEffect(() => {
    const pop = () => { epoch.current += 1; setLocation(popped); };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);
  useEffect(() => {
    const page = view.page;
    if (fixture || !page) return;
    const key = mountKey(location, view);
    if (entered.current === key) return;
    entered.current = key;
    void (async () => {
      for (const trigger of triggers) if (trigger.source === page.root && trigger.event === "mount") await invoke(trigger, {});
    })().catch(report);
  }, [location, view.key, fixture]);
  const Component = view.page ? components[view.page.id] : undefined;
  return <div onClickCapture={event => capture("click", event)} onChangeCapture={event => capture("change", event)}
    onInputCapture={event => capture("input", event)} onSubmitCapture={event => capture("submit", event)}
    onKeyDownCapture={event => capture("keydown", event)} onKeyUpCapture={event => capture("keyup", event)}>
    {Component ? <Component key={view.key} data={display} params={view.params} /> : <main><h1>Page not found</h1></main>}
  </div>;
}
'''


def api_client_source(backend: dict) -> str:
    """Render the canonical client; route parameters accept both IR spellings.

    Each catalog entry carries the request fields the accepted API model declares, so
    the browser rejects an undeclared query, body, header or route parameter before the
    request is sent. A model without a ``request`` object keeps transport-only behaviour
    (``strict: false``): there is nothing to compare against.
    """
    catalog = {}
    for aid, api in backend["apis"].items():
        signature = api["signature"]
        contract = api_contract(api)
        entry = {"method": signature["method"], "path": signature["path"],
            "strict": contract is not None, "params": [], "query": [], "requiredQuery": [],
            "body": [], "requiredBody": [], "headers": []}
        if contract is not None:
            entry.update({"params": sorted(contract["params"]), "query": sorted(contract["query"]),
                "requiredQuery": sorted(contract["required_query"]), "body": sorted(contract["body"]),
                "requiredBody": sorted(contract["required_body"]), "headers": sorted(contract["headers"])})
        catalog[aid] = entry
    prefix = ('import type { UiApiRequest, UiApiResponse } from "./ui-runtime";\n'
        "type ApiContract = { method: string; path: string; strict: boolean; params: string[]; "
        "query: string[]; requiredQuery: string[]; body: string[]; requiredBody: string[]; "
        "headers: string[] };\n"
        "const catalog: Record<string, ApiContract> = " + json.dumps(catalog) + ";\n")
    return prefix + CLIENT_SOURCE.replace("__API_PATH_PARAMETER__", PARAMETER_PATTERN)


def sync_triggers(output, ui, design, backend, active_triggers):
    """Import fixed handle exports directly; placeholders never touch their source."""
    active = set(active_triggers)
    active.intersection_update(design["triggers"])
    save_text(output / "frontend/src/ui-runtime.ts", UI_RUNTIME)
    sync_router(output)
    save_text(output / API_CLIENT, api_client_source(backend))
    imports = ['import type { UiTriggerHandler } from "./ui-runtime";']
    registrations = []
    for number, tid in enumerate(sorted(active)):
        entry = implementation_entrypoint(output, "trigger", tid)
        if entry:
            imported = import_path(TRIGGER_REGISTRY, entry["path"], extension="")
            imports.append(f'import {{ {entry["export"]} as trigger{number} }} from {json.dumps(imported)};')
            registrations.append(f"  {json.dumps(tid)}: trigger{number},")
    save_text(output / TRIGGER_REGISTRY, "\n".join([*imports, "", "export const handlers: Record<string, UiTriggerHandler> = {", *registrations, "};", ""]))
    imports = ['import { useEffect, useRef, useState } from "react";',
        'import type { ComponentType, SyntheticEvent, KeyboardEvent } from "react";',
        'import type { UiPageProps, UiRouteParams, UiTriggerContext } from "./ui-runtime";',
        'import { initialPageData, syncInput } from "./ui-runtime";',
        'import { buildRoute, initialLocation, mountKey, popped, pushed, routeView } from "./ui-router";',
        'import { callApi } from "./ui-api";', 'import { handlers } from "./ui-triggers";']
    pages, components = [], []
    for number, (pid, page) in enumerate(ui["pages"].items()):
        entry = implementation_entrypoint(output, "page", pid)
        if entry:
            imports.append(f'import {{ {entry["export"]} as Page{number} }} from {json.dumps(import_path("frontend/src/App.tsx", entry["path"], extension=""))};')
        else:
            imports.append(f'const Page{number} = (_props: UiPageProps) => <main><h1>UI implementation pending</h1></main>;')
        pages.append({"id": pid, "route": page["route"], "root": page["root"]})
        components.append(f"{json.dumps(pid)}: Page{number}")
    triggers = [{"id": tid, "source": trigger["source"], "event": trigger["event"]}
        for tid, trigger in design["triggers"].items() if tid in active]
    source = "// Compiler-owned interaction state and trigger registration.\n" + "\n".join(imports) + "\n"
    source += "type Trigger = { id: string; source: string; event: string };\n"
    source += "const defaults: Record<string, unknown> = " + json.dumps({var: binding["initial"] for var, binding in ui["bindings"].items()}) + ";\n"
    source += "const inputBindings: Record<string, string[]> = " + json.dumps({uid: node["data"] for uid, node in ui["elements"].items() if "user_write" in node.get("mode", []) and node.get("data")}) + ";\n"
    source += "const pages = " + json.dumps(pages) + ";\nconst components: Record<string, ComponentType<UiPageProps>> = {" + ", ".join(components) + "};\n"
    source += "const triggers: Trigger[] = " + json.dumps(triggers) + ";\n"
    save_text(output / "frontend/src/App.tsx", source + APP_SOURCE)

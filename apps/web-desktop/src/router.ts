import { useMemo, useSyncExternalStore } from 'react';
import type { IconName } from '@server-guardian/ui';
import { tr } from './i18n';

export type RouteId = 'fleet' | 'debates' | 'approvals' | 'settings';

export interface RouteDef {
  id: RouteId;
  /** Navigation label. */
  label: string;
  /** Page header title. */
  title: string;
  icon: IconName;
}

/** The four destinations; the sidebar and the mobile tab bar list all of them. */
export const ROUTES: RouteDef[] = [
  { id: 'fleet', label: tr.shell.nav.routes.fleet.label, title: tr.shell.nav.routes.fleet.title, icon: 'server' },
  { id: 'debates', label: tr.shell.nav.routes.debates.label, title: tr.shell.nav.routes.debates.title, icon: 'chat' },
  { id: 'approvals', label: tr.shell.nav.routes.approvals.label, title: tr.shell.nav.routes.approvals.title, icon: 'check' },
  { id: 'settings', label: tr.shell.nav.routes.settings.label, title: tr.shell.nav.routes.settings.title, icon: 'lock' },
];

export const DEFAULT_ROUTE: RouteId = 'fleet';

/** Tabs of the Fleet page: the first path segment after `#/fleet` (`summary` is the bare `#/fleet`). */
export const FLEET_TABS = [
  { value: 'summary', label: tr.shell.nav.fleetTabs.summary },
  { value: 'nodes', label: tr.shell.nav.fleetTabs.nodes },
  { value: 'ssh', label: tr.shell.nav.fleetTabs.ssh },
  { value: 'commands', label: tr.shell.nav.fleetTabs.commands },
  { value: 'groups', label: tr.shell.nav.fleetTabs.groups },
] as const;

export type FleetTab = (typeof FLEET_TABS)[number]['value'];

/** Fleet tab named by the first path param; anything else (or none) is the summary. */
export function fleetTab(param: string | undefined): FleetTab {
  return FLEET_TABS.find((t) => t.value === param)?.value ?? 'summary';
}

/**
 * Pre-consolidation hashes that keep working. `prefix` is prepended to the remaining path segments and
 * the URL's own query is kept (`#/run?target=a1` -> `#/fleet/commands?target=a1`).
 */
const ALIASES: Record<string, { route: RouteId; prefix?: string[] }> = {
  overview: { route: 'fleet' },
  assistant: { route: 'fleet' },
  more: { route: 'fleet' },
  nodes: { route: 'fleet', prefix: ['nodes'] },
  security: { route: 'fleet', prefix: ['ssh'] },
  ssh: { route: 'fleet', prefix: ['ssh'] },
  run: { route: 'fleet', prefix: ['commands'] },
  batch: { route: 'fleet', prefix: ['commands'] },
  terminal: { route: 'fleet', prefix: ['commands'] },
  agents: { route: 'debates' },
  debate: { route: 'debates' },
};

export interface ParsedRoute {
  route: RouteId;
  /** Path segments after the route, decoded (`#/fleet/nodes/a1` -> ['nodes', 'a1']). */
  params: string[];
  /** Query string pairs (`?target=a1` -> { target: 'a1' }). */
  query: Record<string, string>;
}

function decode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** `#/fleet/nodes/a1?x=1`, `#fleet/nodes` and legacy aliases all parse; unknown or empty hashes fall back to the Fleet summary. */
export function parseHash(hash: string): ParsedRoute {
  const body = hash.replace(/^#\/?/, '');
  const qi = body.indexOf('?');
  const path = qi === -1 ? body : body.slice(0, qi);
  const search = qi === -1 ? '' : body.slice(qi + 1);
  const [head = '', ...rest] = path.split('/').filter((s) => s !== '');

  const query: Record<string, string> = {};
  new URLSearchParams(search).forEach((value, key) => {
    query[key] = value;
  });

  // Node groups moved from Settings to the Fleet Groups tab; the old deep link (`?edit=<id>` kept) still works.
  if (head === 'settings' && rest[0] === 'groups') return { route: 'fleet', params: ['groups'], query };

  const direct = ROUTES.find((r) => r.id === head);
  const alias = direct ? undefined : ALIASES[head];
  const route = direct?.id ?? alias?.route;
  if (!route) return { route: DEFAULT_ROUTE, params: [], query: {} };
  const params = [...(alias?.prefix ?? []), ...rest.map(decode)];
  // `#/agents?run=<id>` (the old monitor link) selects that debate: `#/debates/<id>`.
  if (route === 'debates' && params.length === 0 && query.run) {
    const { run, ...others } = query;
    return { route, params: [run], query: others };
  }
  return { route, params, query };
}

export function hrefFor(id: RouteId, params: string[] = [], query: Record<string, string> = {}): string {
  const path = [id, ...params.map(encodeURIComponent)].join('/');
  const search = new URLSearchParams(query).toString();
  return `#/${path}${search ? `?${search}` : ''}`;
}

/** Href of a Fleet tab (`summary` is the bare `#/fleet`); `rest` are further params such as a node id. */
export function fleetHref(tab: FleetTab, rest: string[] = [], query: Record<string, string> = {}): string {
  return hrefFor('fleet', tab === 'summary' ? [] : [tab, ...rest], query);
}

/** Programmatic navigation; the hashchange listener in useRoute re-renders the app. */
export function navigate(id: RouteId, params: string[] = [], query: Record<string, string> = {}): void {
  window.location.hash = hrefFor(id, params, query);
}

/** Programmatic navigation to a Fleet tab. */
export function navigateFleet(tab: FleetTab, rest: string[] = [], query: Record<string, string> = {}): void {
  window.location.hash = fleetHref(tab, rest, query);
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange);
  return () => window.removeEventListener('hashchange', onChange);
}

/** Current route with params and query, kept in sync with the URL hash. */
export function useRoute(): ParsedRoute {
  const hash = useSyncExternalStore(
    subscribe,
    () => window.location.hash,
    () => ''
  );
  return useMemo(() => parseHash(hash), [hash]);
}

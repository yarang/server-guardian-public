import type { ProjectEntry } from '../types';

/** Project debates and actions belong to when none is chosen (or the chosen one no longer exists); guardian.py's default. */
export const DEFAULT_PROJECT = 'server-guardian';

/** localStorage key of the project chosen in the switcher (per browser). */
export const PROJECT_STORAGE_KEY = 'guardian.project';

/** The stored project id, or DEFAULT_PROJECT when nothing usable is stored or storage is unavailable. */
export function loadStoredProject(): string {
  try {
    const v = window.localStorage.getItem(PROJECT_STORAGE_KEY);
    return v && v.trim() ? v : DEFAULT_PROJECT;
  } catch {
    return DEFAULT_PROJECT;
  }
}

/** Remember the chosen project; storage failures (private mode, quota) are ignored. */
export function storeProject(id: string): void {
  try {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, id);
  } catch {
    // keep the choice for this session only
  }
}

/** What a fleet debate is about: the whole fleet ({}), one node or one group. */
export interface OpsScope {
  node?: string;
  group?: string;
}

/** Builtin ops project (configs/projects/fleet.json): every fleet node except the hub. */
export const FLEET_PROJECT = 'fleet';

/** Whether a project is an ops project (fleet nodes, no repository). */
export function isOpsProject(p: ProjectEntry | null | undefined): boolean {
  return p?.data.kind === 'ops';
}

/** Whether a project has no repository (ops and design projects): no reference code, files or code wiki. */
export function hasNoRepository(p: ProjectEntry | null | undefined): boolean {
  return p?.data.kind === 'ops' || p?.data.kind === 'design';
}

/**
 * The ops project a fleet, node or group debate belongs to: the first valid ops project other than the builtin
 * fleet project that names the group (for a group) or whose resolved targets include the node (for a node), else
 * the builtin fleet project.
 * Undefined when the hub lists no ops project at all (an older hub): the debate then keeps the selected project.
 */
export function opsProjectFor(projects: ProjectEntry[] | undefined, scope: OpsScope = {}): string | undefined {
  const ops = (projects ?? []).filter((p) => isOpsProject(p) && p.errors.length === 0);
  // The catch-all fleet project matches every node, so a more specific project is looked for first.
  const match = ops.find(
    (p) =>
      p.id !== FLEET_PROJECT &&
      (scope.group ? (p.data.targets?.groups ?? []).includes(scope.group) : scope.node ? (p.targets?.ids ?? []).includes(scope.node) : false),
  );
  if (match) return match.id;
  return ops.some((p) => p.id === FLEET_PROJECT) ? FLEET_PROJECT : undefined;
}

/** The ops tools of ops projects (cli/llm-agent/mcp_client.SERVER_TOOLS); `auto` marks those auto_run accepts. */
export const OPS_TOOLS: { name: string; auto: boolean }[] = [
  { name: 'ops:docker_prune', auto: true },
  { name: 'ops:journal_vacuum', auto: true },
  { name: 'ops:apt_clean', auto: true },
  { name: 'ops:remove_paths', auto: false },
  { name: 'ops:truncate_logs', auto: false },
  { name: 'ops:remove_old_kernels', auto: false },
  { name: 'ops:restart_service', auto: false },
  { name: 'jev_mcp:server_remediation', auto: false },
];

/** Tools a project must opt into: package changes and command sets / scripts. Never part of the default set of a new project. */
export const OPS_OPT_IN_TOOLS: { name: string; auto: boolean }[] = [
  { name: 'ops:package_install', auto: false },
  { name: 'ops:package_remove', auto: false },
  { name: 'ops:run_command_set', auto: false },
  { name: 'ops:run_script', auto: false },
];

/** policy.batch defaults and limits the hub enforces (project_registry.py); the hub validates again. */
export const BATCH_DEFAULTS = { maxParallelHosts: 2, maxSteps: 20, maxPlansPerDay: 0 };

/** One id or pattern per line, blanks dropped. */
export function lines(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter(Boolean);
}

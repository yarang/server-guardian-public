import { invoke } from '@tauri-apps/api/core';
import { tr } from './i18n';
import type { CodeFileDetail, CodeFileLines, CodeFileList } from './types';
import {
  ActionItem,
  ArtifactFile,
  ArtifactManifest,
  ArtifactProjectList,
  ArtifactVersionList,
  ProjectResults,
  ActionRunResponse,
  PlanItem,
  PlanRunResponse,
  CloseThreadResponse,
  JevRecheckResponse,
  FleetNode,
  SystemMetrics,
  SSHConfigSummary,
  SecurityReport,
  AuthorizedKey,
  FleetDiagnosisReport,
  FleetSSHReport,
  FleetStreamEvent,
  GroupPreview,
  CommandSet,
  CommandSetList,
  GroupSaveResult,
  HostKey,
  NodeCheckResult,
  NodeConnection,
  NodeGroupData,
  NodeGroupList,
  NodeProbeResult,
  NodeRegisterRequest,
  MeshJoinInfo,
  MeshNodeList,
  MeshRemoveResult,
  MeshJoinRequest,
  MeshJoinResult,
  NodeRegistration,
  NodeRemoveResult,
  NodeUpdateRequest,
  NodeWriteResult,
  PackageRoleList,
  PersonaConfig,
  PersonaList,
  ProjectData,
  ProjectList,
  ProjectSyncResult,
  PublishPlan,
  Publication,
  RevokeKeyPreview,
  RevokeKeyResult,
  AuditEntry,
  WorkspaceResetPlan,
  WorkspaceResetResult,
  PublishThreadResponse,
  SaveResult,
  TeamData,
  TeamList,
  WorkerReport,
  DebateJob,
  DebateJobDetail,
  DebateStartRequest,
  DebateEvent,
  DebateEventsPage,
  DebatePrompt,
  ThreadReport,
  ThreadReportAssetFile,
  DebateStreamMessage,
  WikiBuildRequest,
  WikiList,
  WikiPageDetail,
  WikiSaveRequest,
  WikiSaveResult,
} from './types';


/**
 * The hub serves this dashboard from the agent itself, so its own origin is the agent (and the
 * only one its CSP `connect-src 'self'` allows). The Vite dev server and the Tauri app are not
 * the agent and default to port 9870 on the same host.
 */
function defaultAgentUrl(): string {
  if (typeof window === 'undefined' || !window.location.hostname) return 'http://localhost:9870';
  const { protocol, hostname, port, origin } = window.location;
  const servedByAgent = (protocol === 'http:' || protocol === 'https:') && port !== '5173' && hostname !== 'tauri.localhost';
  return servedByAgent ? origin : `http://${hostname}:9870`;
}

/** Where the agent URL chosen in Settings is kept. It is not a secret; the token is never stored here. */
const AGENT_URL_KEY = 'guardian_agent_url';

function servedByAgent(): boolean {
  if (typeof window === 'undefined' || !window.location.hostname || inTauri()) return false;
  return defaultAgentUrl() === window.location.origin;
}

/**
 * The URL saved in Settings, else the default. The dashboard served by the agent ignores it: its
 * CSP only allows its own origin, so a saved URL would lock it out until storage is cleared.
 */
function initialAgentUrl(): string {
  if (!servedByAgent()) {
    try {
      const saved = localStorage.getItem(AGENT_URL_KEY);
      if (saved) return saved;
    } catch {
      // Storage unavailable: fall back to the default.
    }
  }
  return defaultAgentUrl();
}

let AGENT_BASE_URL = initialAgentUrl();

/** Points the app at another agent and remembers it across restarts. */
export function setAgentUrl(url: string) {
  AGENT_BASE_URL = url.replace(/\/$/, '');
  try {
    localStorage.setItem(AGENT_URL_KEY, AGENT_BASE_URL);
  } catch {
    // Not persisted: the URL lasts until the app closes.
  }
  tokenReady = loadToken();
  signedOut = false;
}

export function getAgentUrl() {
  return AGENT_BASE_URL;
}

// --- Authentication -----------------------------------------------------------
// The hub dashboard is served by the agent itself, so it authenticates with an HttpOnly
// session cookie and never holds the token. A cross-origin client sends the token as a bearer
// header and keeps it in memory only; the Tauri app persists it in the OS credential store
// (src-tauri/src/token_store.rs), one entry per agent. The token never goes to localStorage.

/** Where earlier versions stored the token in plain text; migrated and removed on load. */
const LEGACY_TOKEN_KEY = 'guardian_agent_token';

export class AuthRequiredError extends Error {}

let memoryToken: string | null = null;

function isSameOrigin(): boolean {
  try {
    return typeof window !== 'undefined' && new URL(AGENT_BASE_URL).origin === window.location.origin;
  } catch {
    return false;
  }
}

function inTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

function takeLegacyToken(): string | null {
  try {
    const legacy = localStorage.getItem(LEGACY_TOKEN_KEY);
    localStorage.removeItem(LEGACY_TOKEN_KEY);
    return legacy;
  } catch {
    return null;
  }
}

/** Loads the token for the current agent; a token left in localStorage moves to the keychain. */
async function loadToken(): Promise<void> {
  memoryToken = null;
  if (!inTauri()) {
    takeLegacyToken();
    return;
  }
  if (isSameOrigin()) return;
  const legacy = takeLegacyToken();
  const agent = AGENT_BASE_URL;
  try {
    let token = await invoke<string | null>('token_get', { agent });
    if (!token && legacy) {
      await invoke('token_set', { agent, token: legacy });
      token = legacy;
    }
    if (agent === AGENT_BASE_URL) memoryToken = token ?? null;
  } catch {
    // Credential store unavailable: the user signs in again and the token stays in memory.
  }
}

let tokenReady: Promise<void> = loadToken();

/**
 * Set when the hub answers 401. While set, authFetch issues no request at all, so every poller
 * (health chip, metrics, approvals, debates, fleet, operations) goes quiet behind the sign-in
 * dialog. Cleared by a successful login, by pointing at another hub, or by the dialog being left
 * for Settings; the caller then refetches once (the global Refresh token).
 */
let signedOut = false;

export function isSignedOut(): boolean {
  return signedOut;
}

/** Lets requests flow again (the next 401 sets the flag back). */
export function resumeRequests(): void {
  signedOut = false;
}

export async function authFetch(url: string, init: RequestInit = {}): Promise<Response> {
  if (signedOut) {
    window.dispatchEvent(new CustomEvent('guardian:auth-required'));
    throw new AuthRequiredError(tr.shell.api.authRequired);
  }
  await tokenReady;
  const headers = new Headers(init.headers);
  const token = isSameOrigin() ? null : memoryToken;
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const res = await fetch(url, { ...init, headers, credentials: 'same-origin' });
  if (res.status === 401) {
    signedOut = true;
    window.dispatchEvent(new CustomEvent('guardian:auth-required'));
    throw new AuthRequiredError(tr.shell.api.authRequired);
  }
  return res;
}

export async function login(token: string): Promise<boolean> {
  const res = await fetch(`${AGENT_BASE_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
    credentials: 'same-origin',
  });
  if (!res.ok) return false;
  signedOut = false;
  takeLegacyToken();
  if (isSameOrigin()) {
    memoryToken = null;
    return true;
  }
  memoryToken = token;
  if (inTauri()) {
    try {
      await invoke('token_set', { agent: AGENT_BASE_URL, token });
    } catch {
      // Not persisted: the session lasts until the app closes.
    }
  }
  return true;
}

export async function logout(): Promise<void> {
  memoryToken = null;
  takeLegacyToken();
  if (inTauri()) {
    await invoke('token_delete', { agent: AGENT_BASE_URL }).catch(() => undefined);
  }
  await fetch(`${AGENT_BASE_URL}/api/auth/logout`, { method: 'POST', credentials: 'same-origin' }).catch(() => undefined);
}

export interface AgentHealth {
  /** `signed-out`: the hub answered 401, so the session is missing or expired (not an outage). */
  state: 'online' | 'degraded' | 'offline' | 'signed-out';
  /** Why the agent reported itself degraded (unreadable metrics, full disk, …). */
  reasons: string[];
}

/** The agent's own verdict from /api/status: reachable and healthy, reachable but degraded, or unreachable. */
export async function checkAgentHealth(): Promise<AgentHealth> {
  try {
    const res = await authFetch(`${AGENT_BASE_URL}/api/status`, { signal: AbortSignal.timeout(2000) });
    if (!res.ok) return { state: 'offline', reasons: [`HTTP ${res.status}`] };
    const body = (await res.json().catch(() => ({}))) as { status?: string; reasons?: string[] };
    if (body.status === 'healthy') return { state: 'online', reasons: [] };
    return { state: 'degraded', reasons: body.reasons?.length ? body.reasons : [`status ${body.status ?? 'unknown'}`] };
  } catch (err) {
    if (err instanceof AuthRequiredError) return { state: 'signed-out', reasons: [] };
    return { state: 'offline', reasons: [] };
  }
}

export async function fetchMetrics(): Promise<SystemMetrics> {
  return hubRequest<SystemMetrics>('/api/metrics', {}, 5000);
}

export async function fetchSSHConfig(): Promise<SSHConfigSummary> {
  return hubRequest<SSHConfigSummary>('/api/ssh/config', {}, 5000);
}

export async function fetchSSHSecurity(): Promise<SecurityReport> {
  return hubRequest<SecurityReport>('/api/ssh/security', {}, 5000);
}

export async function fetchAuthorizedKeys(): Promise<AuthorizedKey[]> {
  const data = await hubRequest<{ keys?: AuthorizedKey[] }>('/api/ssh/keys', {}, 5000);
  return data.keys || [];
}

/**
 * Per-node sshd audit over the fleet SSH key (all nodes, or `targets`). The hub caches results
 * for 60s per target set; `refresh` probes again. Slow nodes take up to 20s each, 8 at a time.
 */
export async function fetchFleetSSHAudit(targets: string[] = [], refresh = false): Promise<FleetSSHReport> {
  const params = new URLSearchParams();
  if (targets.length) params.set('targets', targets.join(','));
  if (refresh) params.set('refresh', '1');
  const query = params.toString();
  return hubRequest<FleetSSHReport>(`/api/fleet/ssh${query ? `?${query}` : ''}`, {}, 95000);
}

export async function executeCommand(cmd: string): Promise<CommandResult> {
  return runCommand('/api/exec', { command: cmd, timeout_sec: 15 });
}

export async function fetchServers(): Promise<FleetNode[]> {
  const data = await hubRequest<{ nodes?: FleetNode[] }>('/api/nodes', {}, 5000);
  return data.nodes || [];
}

export async function executeRemoteSSH(targetNode: string, cmd: string): Promise<CommandResult> {
  return runCommand('/api/exec/remote', { target_host: targetNode, command: cmd, timeout_sec: 15 });
}

export interface CommandResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  duration_ms: number;
}

/**
 * Runs a command through the hub. A failed request is reported as exit code -1 with the reason in
 * stderr; it is never shown as a successful run.
 */
async function runCommand(path: string, body: Record<string, unknown>): Promise<CommandResult> {
  try {
    return await hubRequest<CommandResult>(path, { method: 'POST', body: JSON.stringify(body) }, 60000);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return { exit_code: -1, stdout: '', stderr: tr.shell.api.commandNotRun(message), duration_ms: 0 };
  }
}

export async function diagnoseFleet(): Promise<FleetDiagnosisReport> {
  return hubRequest<FleetDiagnosisReport>('/api/fleet/diagnose', {}, 30000);
}


/** Per-node timeout for fleet runs; the hub's own fallback is 10 s, so always send it explicitly. */
export const DEFAULT_FLEET_TIMEOUT_SEC = 15;

/** Splits an SSE buffer into complete events; the unfinished tail is returned as `rest`. */
export function parseSSE(buffer: string): { events: { event: string; data: string }[]; rest: string } {
  const events: { event: string; data: string }[] = [];
  const blocks = buffer.replace(/\r\n/g, '\n').split('\n\n');
  const rest = blocks.pop() ?? '';
  for (const block of blocks) {
    let event = 'message';
    const data: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
    if (data.length > 0) events.push({ event, data: data.join('\n') });
  }
  return { events, rest };
}

/**
 * Runs a command across the fleet and streams progress events. The command goes in a POST
 * body read with fetch (not EventSource), so it never lands in a URL and the token travels
 * in the Authorization header. Returns a cancel function.
 */
export function streamFleetExec(
  command: string,
  targets: string[] = [],
  onEvent: (event: FleetStreamEvent) => void,
  onError: (err: any) => void,
  timeoutSec: number = DEFAULT_FLEET_TIMEOUT_SEC
): () => void {
  const controller = new AbortController();
  let finished = false;

  const deliver = (raw: { event: string; data: string }) => {
    let parsed: FleetStreamEvent;
    try {
      parsed = JSON.parse(raw.data);
    } catch {
      return;
    }
    if (raw.event === 'complete' || raw.event === 'error') finished = true;
    onEvent(parsed);
  };

  (async () => {
    const res = await authFetch(`${AGENT_BASE_URL}/api/fleet/exec/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(targets.length > 0 ? { command, targets, timeout_sec: timeoutSec } : { command, timeout_sec: timeoutSec }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      let message = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body?.error) message = body.error;
      } catch {
        // Not JSON: keep the status line.
      }
      throw new Error(message);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSSE(buffer);
      buffer = parsed.rest;
      parsed.events.forEach(deliver);
    }
    parseSSE(buffer + decoder.decode() + '\n\n').events.forEach(deliver);
    if (!finished) throw new Error(tr.shell.api.streamEnded);
  })().catch((err) => {
    if (controller.signal.aborted) return;
    onError(err);
  });

  return () => controller.abort();
}

// --- Debate / persona / team API -------------------------------------------
// No fallbacks: every failure surfaces as an Error carrying the server's message.

export class HubError extends Error {
  status: number;
  /** Stable machine code of a structured error (`{error, code, field?, hint?}`), when the hub sent one. */
  code?: string;
  /** Request field the failure is about (`hostname`, `metadata.tier`, ...). */
  field?: string;
  /** Optional action the operator can take, or the stderr tail for `unreachable`. */
  hint?: string;
  /** Reachability detail of a failed key revoke (`{error, reachability}`), when the hub sent one. */
  reachability?: string;
  /** Host keys a `fingerprint_mismatch` response carries. */
  keys?: HostKey[];
  constructor(message: string, status: number, details: { code?: string; field?: string; hint?: string; reachability?: string; keys?: HostKey[] } = {}) {
    super(message);
    this.status = status;
    this.code = details.code;
    this.field = details.field;
    this.hint = details.hint;
    this.reachability = details.reachability;
    this.keys = details.keys;
  }
}

/** Reads the optional structured keys of an error body; anything of another type is ignored. */
function errorDetails(body: any): { code?: string; field?: string; hint?: string; reachability?: string; keys?: HostKey[] } {
  if (!body || typeof body !== 'object') return {};
  return {
    code: typeof body.code === 'string' ? body.code : undefined,
    field: typeof body.field === 'string' ? body.field : undefined,
    hint: typeof body.hint === 'string' ? body.hint : undefined,
    reachability:
      typeof body.reachability === 'string' ? body.reachability : body.reachability && typeof body.reachability === 'object' ? JSON.stringify(body.reachability) : undefined,
    keys: Array.isArray(body.keys) ? (body.keys as HostKey[]) : undefined,
  };
}

async function hubRequest<T>(path: string, init: RequestInit = {}, timeoutMs: number | null = 15000): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set('Content-Type', 'application/json');
  let res: Response;
  try {
    const opts: RequestInit = { ...init, headers };
    if (timeoutMs !== null) opts.signal = AbortSignal.timeout(timeoutMs);
    res = await authFetch(`${AGENT_BASE_URL}${path}`, opts);
  } catch (e) {
    if (e instanceof AuthRequiredError) throw e;
    throw new Error(tr.shell.api.cannotReach(e instanceof Error ? e.message : String(e)));
  }
  const text = await res.text();
  let body: any = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!res.ok) {
    if (res.status === 404 && !(body && typeof body.error === 'string')) {
      // The agent's handlers answer 404 with a JSON error; a plain one means the route does not exist.
      throw new HubError(
        tr.shell.api.routeMissing(AGENT_BASE_URL, path.split('?')[0]),
        404
      );
    }
    const msg = body && typeof body.error === 'string' && body.error ? body.error : text.trim().slice(0, 200);
    throw new HubError(msg || `HTTP ${res.status}`, res.status, errorDetails(body));
  }
  return body as T;
}

/** The roles of configs/package-roles.json (read-only): nodes pick roles by name. */
export function fetchPackageRoles(): Promise<PackageRoleList> {
  return hubRequest<PackageRoleList>('/api/package-roles');
}

export function fetchPersonas(): Promise<PersonaList> {
  return hubRequest<PersonaList>('/api/personas');
}

export function savePersona(persona: PersonaConfig): Promise<SaveResult> {
  const { source: _source, ...payload } = persona;
  return hubRequest<{ ok: boolean; id: string; warnings?: string[] }>('/api/personas', {
    method: 'POST',
    body: JSON.stringify(payload),
  }).then((r) => ({ id: r.id, warnings: r.warnings || [] }));
}

export function deletePersona(id: string): Promise<void> {
  return hubRequest<unknown>(`/api/personas/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(() => undefined);
}

export function fetchTeams(): Promise<TeamList> {
  return hubRequest<TeamList>('/api/teams');
}

export function fetchWorkers(): Promise<WorkerReport> {
  return hubRequest<WorkerReport>('/api/workers');
}

export function saveTeam(team: TeamData): Promise<SaveResult> {
  return hubRequest<{ ok: boolean; id: string; warnings?: string[] }>('/api/teams', {
    method: 'POST',
    body: JSON.stringify(team),
  }).then((r) => ({ id: r.id, warnings: r.warnings || [] }));
}

export function deleteTeam(id: string): Promise<void> {
  return hubRequest<unknown>(`/api/teams/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(() => undefined);
}

export function fetchCommandSets(): Promise<CommandSetList> {
  return hubRequest<CommandSetList>('/api/command-sets');
}

export function saveCommandSet(set: Omit<CommandSet, 'source' | 'reviewed' | 'confirmed'>): Promise<SaveResult> {
  return hubRequest<{ ok: boolean; id: string; warnings?: string[] }>('/api/command-sets', {
    method: 'POST',
    body: JSON.stringify(set),
  }).then((r) => ({ id: r.id, warnings: r.warnings || [] }));
}

export function deleteCommandSet(id: string): Promise<void> {
  return hubRequest<unknown>(`/api/command-sets/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(() => undefined);
}

export function confirmCommandSet(id: string): Promise<void> {
  return hubRequest<unknown>(`/api/command-sets/${encodeURIComponent(id)}/confirm`, { method: 'POST' }).then(() => undefined);
}

export function startDebate(req: DebateStartRequest): Promise<DebateJob> {
  return hubRequest<DebateJob>('/api/debates', { method: 'POST', body: JSON.stringify(req) });
}

export async function fetchDebates(project?: string): Promise<DebateJob[]> {
  const q = project ? `?project=${encodeURIComponent(project)}` : '';
  const r = await hubRequest<{ debates: DebateJob[] | null }>(`/api/debates${q}`);
  return r.debates || [];
}

export function fetchProjects(): Promise<ProjectList> {
  return hubRequest<ProjectList>('/api/projects');
}

export function saveProject(project: ProjectData): Promise<SaveResult> {
  return hubRequest<{ ok: boolean; id: string; warnings?: string[] }>('/api/projects', {
    method: 'POST',
    body: JSON.stringify(project),
  }).then((r) => ({ id: r.id, warnings: r.warnings || [] }));
}

export function deleteProject(id: string): Promise<void> {
  return hubRequest<unknown>(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(() => undefined);
}

/** What resetting the project's workspace would discard and keep (read-only). */
export function getWorkspaceReset(id: string): Promise<WorkspaceResetPlan> {
  return hubRequest<WorkspaceResetPlan>(`/api/projects/${encodeURIComponent(id)}/workspace-reset`);
}

/** Discards the uncommitted workspace changes listed by {@link getWorkspaceReset}. 409 busy, 403 when UI actions are off. */
export function resetWorkspace(id: string, release: string[] = []): Promise<WorkspaceResetResult> {
  return hubRequest<WorkspaceResetResult>(
    `/api/projects/${encodeURIComponent(id)}/workspace-reset`,
    { method: 'POST', body: JSON.stringify(release.length ? { confirm: true, release } : { confirm: true }) },
    60000
  );
}

/** Clones or fast-forwards the project's workspace on the hub (can take minutes for a first clone). */
export async function syncProject(id: string): Promise<ProjectSyncResult> {
  const r = await hubRequest<{ results: ProjectSyncResult[] }>(`/api/projects/${encodeURIComponent(id)}/sync`, { method: 'POST' }, null);
  return r.results[0];
}

// --- Code wiki -------------------------------------------------------------------
// Pages live on the hub; states are computed there against the project workspace's HEAD (or `sha`).

const wikiPath = (project: string, page?: string) =>
  `/api/projects/${encodeURIComponent(project)}/wiki${page === undefined ? '' : `/${encodeURIComponent(page)}`}`;

/** The project's pages with their state; the first call on a large repository builds the code index (up to a minute). */
export function fetchWikiPages(project: string, sha?: string): Promise<WikiList> {
  return hubRequest<WikiList>(`${wikiPath(project)}${sha ? `?sha=${encodeURIComponent(sha)}` : ''}`, {}, 120000);
}

export function fetchWikiPage(project: string, page: string, sha?: string): Promise<WikiPageDetail> {
  return hubRequest<WikiPageDetail>(`${wikiPath(project, page)}${sha ? `?sha=${encodeURIComponent(sha)}` : ''}`, {}, 120000);
}

/** Saves a person's version of a page (creates it when new). 413 when the text is over 256 KB. */
export function saveWikiPage(project: string, page: string, req: WikiSaveRequest): Promise<WikiSaveResult> {
  return hubRequest<WikiSaveResult>(wikiPath(project, page), { method: 'PUT', body: JSON.stringify(req) }, 120000);
}

export function deleteWikiPage(project: string, page: string): Promise<void> {
  return hubRequest<unknown>(wikiPath(project, page), { method: 'DELETE' }, 60000).then(() => undefined);
}

/** Starts a wiki build job; follow it with the Agents monitor. 409 when this project is already building, 429 when the build slot is taken. */
export function startWikiBuild(project: string, req: WikiBuildRequest): Promise<DebateJob> {
  return hubRequest<DebateJob>(`${wikiPath(project)}/build`, { method: 'POST', body: JSON.stringify(req) }, 30000);
}

// --- Code files (file view of the wiki) -----------------------------------------
// Made on the hub without an LLM from the symbol index; reading lines is audited there.

const filesPath = (project: string) => `/api/projects/${encodeURIComponent(project)}/files`;

function fileQuery(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : '';
}

export function fetchCodeFiles(project: string, sha?: string): Promise<CodeFileList> {
  return hubRequest<CodeFileList>(`${filesPath(project)}${fileQuery({ sha })}`, {}, 120000);
}

export function fetchCodeFile(project: string, path: string, sha?: string): Promise<CodeFileDetail> {
  return hubRequest<CodeFileDetail>(`${filesPath(project)}/show${fileQuery({ path, sha })}`, {}, 120000);
}

/** Lines start..end (at most 400). */
export function fetchCodeFileLines(project: string, path: string, start: number, end: number, sha?: string): Promise<CodeFileLines> {
  return hubRequest<CodeFileLines>(`${filesPath(project)}/lines${fileQuery({ path, start, end, sha })}`, {}, 60000);
}

// --- Node groups ---------------------------------------------------------------
// The hub resolves members against servers.json; execution endpoints get the resolved ids.

export function fetchGroups(): Promise<NodeGroupList> {
  return hubRequest<NodeGroupList>('/api/groups');
}

export function saveGroup(group: NodeGroupData): Promise<GroupSaveResult> {
  return hubRequest<{ ok: boolean } & GroupSaveResult>('/api/groups', {
    method: 'POST',
    body: JSON.stringify(group),
  }).then((r) => ({ id: r.id, members: r.members || [], warnings: r.warnings || [] }));
}

/** Resolves a (possibly unsaved) definition's members without saving it. */
export function previewGroup(group: Omit<NodeGroupData, 'name'> & { name?: string }): Promise<GroupPreview> {
  return hubRequest<{ ok: boolean } & GroupPreview>('/api/groups/preview', {
    method: 'POST',
    body: JSON.stringify(group),
  }).then((r) => ({ members: r.members || [], warnings: r.warnings || [] }));
}

export function deleteGroup(id: string): Promise<void> {
  return hubRequest<unknown>(`/api/groups/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(() => undefined);
}

// --- Mesh join -------------------------------------------------------------------
// The hub approves a new host's headscale login over its fleet SSH (docs/design/mesh-join.md).

export function fetchMeshJoin(): Promise<MeshJoinInfo> {
  return hubRequest<MeshJoinInfo>('/api/mesh/join', {}, 40000);
}

export function joinMesh(req: MeshJoinRequest): Promise<MeshJoinResult> {
  return hubRequest<MeshJoinResult>('/api/mesh/join', { method: 'POST', body: JSON.stringify(req) }, 45000);
}

export function fetchMeshNodes(): Promise<MeshNodeList> {
  return hubRequest<MeshNodeList>('/api/mesh/nodes', {}, 40000);
}

/** Deletes a node from headscale; `confirmName` must equal its name. */
export function removeMeshNode(id: number, confirmName: string): Promise<MeshRemoveResult> {
  return hubRequest<MeshRemoveResult>(`/api/mesh/nodes/${id}/remove`, { method: 'POST', body: JSON.stringify({ confirm_name: confirmName }) }, 70000);
}

// --- Node registration -----------------------------------------------------------
// The hub writes its SSH fleet file and inventory; failures carry HubError.code / .field / .hint.

export function fetchNodeRegistration(): Promise<NodeRegistration> {
  return hubRequest<NodeRegistration>('/api/nodes/registration');
}

/** Validates a destination and scans its host keys on the hub; writes nothing. */
export function checkNode(conn: NodeConnection): Promise<NodeCheckResult> {
  return hubRequest<NodeCheckResult>('/api/nodes/check', { method: 'POST', body: JSON.stringify(conn) }, 30000);
}

/** Registers a node (pins the confirmed host key) and probes it. */
export function registerNode(req: NodeRegisterRequest): Promise<NodeWriteResult> {
  return hubRequest<NodeWriteResult>('/api/nodes', { method: 'POST', body: JSON.stringify(req) }, 45000);
}

export function updateNode(id: string, req: NodeUpdateRequest): Promise<NodeWriteResult> {
  return hubRequest<NodeWriteResult>(`/api/nodes/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(req) }, 45000);
}

export function deleteNode(id: string): Promise<NodeRemoveResult> {
  return hubRequest<NodeRemoveResult>(`/api/nodes/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

/** Re-runs `ssh <id> uname -m` on the hub; a successful probe stores the node's arch. */
export function probeNode(id: string): Promise<NodeProbeResult> {
  return hubRequest<NodeProbeResult>(`/api/nodes/${encodeURIComponent(id)}/probe`, { method: 'POST' }, 30000);
}

export function fetchDebate(id: string): Promise<DebateJobDetail> {
  return hubRequest<DebateJobDetail>(`/api/debates/${encodeURIComponent(id)}`);
}

export function cancelDebate(id: string): Promise<void> {
  return hubRequest<unknown>(`/api/debates/${encodeURIComponent(id)}`, { method: 'DELETE' }).then(() => undefined);
}

// --- Debate events (Agents monitor) ---------------------------------------------

/** The hub returns at most this many events per poll page. */
export const EVENTS_PAGE_SIZE = 1000;

function isEvent(v: unknown): v is DebateEvent {
  if (!v || typeof v !== 'object') return false;
  const e = v as { seq?: unknown; type?: unknown };
  return typeof e.seq === 'number' && Number.isFinite(e.seq) && typeof e.type === 'string';
}

/** Events with `seq > after` (up to 1000). A finished job with more events reports done:false; keep paging from `next`. */
export async function pollDebateEvents(id: string, after: number): Promise<DebateEventsPage> {
  const r = await hubRequest<{ events?: unknown[] | null; next?: number; done?: boolean }>(
    `/api/debates/${encodeURIComponent(id)}/events?after=${Math.max(0, Math.floor(after))}`
  );
  const events = (r.events || []).filter(isEvent);
  return { events, next: typeof r.next === 'number' ? r.next : after, done: r.done === true };
}

/** The prompt an agent received at the start of its turn (only for events with `prompt_saved`). */
export async function fetchDebatePrompt(id: string, seq: number): Promise<DebatePrompt> {
  const r = await hubRequest<{ seq?: number; bytes?: number; text?: string }>(
    `/api/debates/${encodeURIComponent(id)}/prompts/${Math.max(0, Math.floor(seq))}`
  );
  return { seq: typeof r.seq === 'number' ? r.seq : seq, bytes: typeof r.bytes === 'number' ? r.bytes : 0, text: typeof r.text === 'string' ? r.text : '' };
}

/** The Markdown result document of a thread. Rejects with a 404 HubError when the thread has no report. */
export async function fetchThreadReport(thread: string): Promise<ThreadReport> {
  const r = await hubRequest<Record<string, unknown>>(`/api/threads/${encodeURIComponent(thread)}/report`, {}, 30000);
  const assets = Array.isArray(r?.assets) ? r.assets : [];
  const markdown = typeof r?.markdown === 'string' ? r.markdown : '';
  return {
    thread_id: typeof r?.thread_id === 'string' && r.thread_id ? r.thread_id : thread,
    project: typeof r?.project === 'string' ? r.project : '',
    markdown,
    bytes: typeof r?.bytes === 'number' ? r.bytes : markdown.length,
    updated_at: typeof r?.updated_at === 'string' ? r.updated_at : '',
    assets: assets
      .filter((a): a is { name: string; bytes?: unknown } => !!a && typeof (a as { name?: unknown }).name === 'string')
      .map((a) => ({ name: a.name, bytes: typeof a.bytes === 'number' ? a.bytes : 0 })),
  };
}

/** One diagram (SVG text) of a thread report. The text is untrusted: sanitize before showing it. */
export async function fetchThreadReportAsset(thread: string, name: string): Promise<ThreadReportAssetFile> {
  const r = await hubRequest<Record<string, unknown>>(
    `/api/threads/${encodeURIComponent(thread)}/report/assets/${encodeURIComponent(name)}`,
    {},
    30000
  );
  const text = typeof r?.text === 'string' ? r.text : '';
  return { name: typeof r?.name === 'string' && r.name ? r.name : name, bytes: typeof r?.bytes === 'number' ? r.bytes : text.length, text };
}

const artifactBase = (thread: string, version?: number) =>
  `/api/threads/${encodeURIComponent(thread)}/artifacts${version === undefined ? '' : `/v${version}`}`;

/** Threads of a design project that have artifacts, newest first. */
export function fetchProjectArtifacts(project: string): Promise<ArtifactProjectList> {
  return hubRequest<ArtifactProjectList>(`/api/projects/${encodeURIComponent(project)}/artifacts`);
}

/** What a project produced, one row per thread: debate report and design artifacts, newest first. */
export function fetchProjectResults(project: string): Promise<ProjectResults> {
  return hubRequest<ProjectResults>(`/api/projects/${encodeURIComponent(project)}/results`);
}

export function fetchArtifactVersions(thread: string): Promise<ArtifactVersionList> {
  return hubRequest<ArtifactVersionList>(artifactBase(thread));
}

export function fetchArtifactManifest(thread: string, version: number): Promise<ArtifactManifest> {
  return hubRequest<ArtifactManifest>(artifactBase(thread, version));
}

/** The authenticated response of one artifact file; a failure becomes a HubError with the hub's message. */
async function fetchArtifactResponse(thread: string, version: number, name: string, signal?: AbortSignal): Promise<Response> {
  let res: Response;
  try {
    res = await authFetch(`${AGENT_BASE_URL}${artifactBase(thread, version)}/files/${encodeURIComponent(name)}`, {
      signal: signal ?? AbortSignal.timeout(30000),
    });
  } catch (e) {
    if (e instanceof AuthRequiredError) throw e;
    throw new Error(tr.shell.api.cannotReach(e instanceof Error ? e.message : String(e)));
  }
  if (res.ok) return res;
  const text = await res.text();
  let msg = '';
  try {
    const body = JSON.parse(text);
    if (body && typeof body.error === 'string') msg = body.error;
  } catch {
    // not JSON: fall through to the plain text
  }
  throw new HubError(msg || text.trim().slice(0, 200) || `HTTP ${res.status}`, res.status);
}

/**
 * The text of one artifact file. The hub sends it with a sandboxing CSP and `nosniff`; the caller shows it as
 * Markdown or as an `<img>` data URL, never as HTML. Authenticated like any other call (cookie or bearer), so no
 * token ever goes into a URL.
 */
export async function fetchArtifactText(thread: string, version: number, name: string, signal?: AbortSignal): Promise<string> {
  return (await fetchArtifactResponse(thread, version, name, signal)).text();
}

/** The bytes of one artifact file (a derived Office file), for saving only: it is never shown on screen. */
export async function fetchArtifactBlob(thread: string, version: number, name: string, signal?: AbortSignal): Promise<Blob> {
  return (await fetchArtifactResponse(thread, version, name, signal)).blob();
}

/** Result of `POST /api/threads/{thread}/artifacts/{version}/convert`. */
export interface ArtifactConvertResult {
  source: string;
  source_sha256: string;
  result_sha256: string;
  file: ArtifactFile;
}

/**
 * Asks the hub to derive a DOCX or PPTX from a stored Markdown file of one version (docs/design/office-export.md). The
 * file is added to that same version; the hub refuses when it already exists.
 */
export function convertArtifact(thread: string, version: number, name: string, format: 'docx' | 'pptx' = 'docx'): Promise<ArtifactConvertResult> {
  return hubRequest<ArtifactConvertResult>(`${artifactBase(thread, version)}/convert`, { method: 'POST', body: JSON.stringify({ name, format }) }, 60000);
}

/** Result of `POST /api/threads/{thread}/report/keep`. */
export interface ReportKeepResult {
  version: number;
  sha256: string;
  bytes: number;
  /** Validator warnings that did not block the snapshot, e.g. report images that are not artifacts. */
  warnings: string[];
  file: ArtifactFile;
}

/**
 * Stores the finished report of a thread as `report.md` in a new artifact version (docs/design/office-export.md, option (c)),
 * so the Word and PowerPoint conversions reach it. The hub re-reads the report itself and refuses (400) one the artifact
 * validators reject; 409 means the latest version already holds this exact text.
 */
export function keepReport(thread: string): Promise<ReportKeepResult> {
  return hubRequest<ReportKeepResult>(`/api/threads/${encodeURIComponent(thread)}/report/keep`, { method: 'POST', body: '{}' }, 60000);
}

export interface DebateStreamOptions {
  /** Connection state changes: `live` once connected, `reconnecting` after a drop. */
  onConnection?: (state: 'live' | 'reconnecting') => void;
  /** First retry delay in ms (doubles up to `retryMaxMs`). */
  retryBaseMs?: number;
  retryMaxMs?: number;
}

export interface DebateStreamResult {
  /** The hub sent its final `end` line. False only when `signal` aborted the stream. */
  ended: boolean;
  status?: string;
  lastSeq: number;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const done = () => {
      window.clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    };
    const timer = window.setTimeout(done, ms);
    signal.addEventListener('abort', done);
  });
}

/**
 * Follows a job's events as NDJSON over fetch (not EventSource, so the auth header / cookie of
 * every other call applies). Each parsed line goes to `onEvent`: events, `heartbeat` and `end`.
 * A dropped connection, or a close without `end` (the hub caps a stream at the job timeout),
 * reconnects with `after=<last seq>` and backoff until `end` arrives or `signal` aborts. Resolves
 * then; rejects on sign-in required or a 4xx answer (job gone, bad id), which retrying cannot fix.
 */
export async function streamDebateEvents(
  id: string,
  after: number,
  onEvent: (message: DebateStreamMessage) => void,
  signal: AbortSignal,
  options: DebateStreamOptions = {}
): Promise<DebateStreamResult> {
  const { onConnection, retryBaseMs = 1000, retryMaxMs = 10000 } = options;
  let last = after;
  let failures = 0;

  const handleLine = (line: string): DebateStreamResult | null => {
    const text = line.trim();
    if (!text) return null;
    let msg: unknown;
    try {
      msg = JSON.parse(text);
    } catch {
      return null;
    }
    if (!msg || typeof msg !== 'object') return null;
    const type = (msg as { type?: unknown }).type;
    if (type === 'heartbeat') {
      onEvent({ type: 'heartbeat' });
    } else if (type === 'end') {
      const status = (msg as { status?: unknown }).status;
      const result = { ended: true, status: typeof status === 'string' ? status : undefined, lastSeq: last };
      onEvent({ type: 'end', status: result.status });
      return result;
    } else if (isEvent(msg)) {
      if (msg.seq <= last) return null; // replayed after a reconnect
      last = msg.seq;
      onEvent(msg);
    }
    return null;
  };

  while (!signal.aborted) {
    try {
      const res = await authFetch(`${AGENT_BASE_URL}/api/debates/${encodeURIComponent(id)}/events?after=${last}&follow=1`, {
        headers: { Accept: 'application/x-ndjson' },
        signal,
      });
      if (!res.ok || !res.body) {
        let message = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body?.error) message = body.error;
        } catch {
          // Not JSON: keep the status line.
        }
        if (res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429) throw new HubError(message, res.status);
        throw new Error(message);
      }
      onConnection?.('live');
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        failures = 0;
        buffer += decoder.decode(value, { stream: true });
        let nl = buffer.indexOf('\n');
        while (nl !== -1) {
          const ended = handleLine(buffer.slice(0, nl));
          if (ended) {
            void reader.cancel().catch(() => undefined);
            return ended;
          }
          buffer = buffer.slice(nl + 1);
          nl = buffer.indexOf('\n');
        }
      }
      const ended = handleLine(buffer + decoder.decode());
      if (ended) return ended;
    } catch (err) {
      if (signal.aborted) break;
      if (err instanceof AuthRequiredError || err instanceof HubError) throw err;
    }
    if (signal.aborted) break;
    onConnection?.('reconnecting');
    await sleep(Math.min(retryBaseMs * 2 ** failures, retryMaxMs), signal);
    failures += 1;
  }
  return { ended: false, lastSeq: last };
}

// --- Judge action approval ---------------------------------------------------

export async function listActions(thread?: string, project?: string): Promise<ActionItem[]> {
  const params = new URLSearchParams();
  if (thread) params.set('thread', thread);
  if (project) params.set('project', project);
  const q = params.toString() ? `?${params.toString()}` : '';
  const r = await hubRequest<{ actions: ActionItem[] | null }>(`/api/actions${q}`);
  return r.actions || [];
}

export async function getAction(id: number): Promise<ActionItem> {
  const r = await hubRequest<{ action: ActionItem }>(`/api/actions/${id}`);
  return r.action;
}

// Synchronous on the server (up to ~15 minutes): no client-side timeout.
export function runAction(id: number): Promise<ActionRunResponse> {
  return hubRequest<ActionRunResponse>(
    `/api/actions/${id}/run`,
    { method: 'POST', body: JSON.stringify({ confirm_id: id }) },
    null
  );
}

/**
 * Mark a RUNNING action whose run died (hub restart, timeout) as FAILED. The hub refuses (409) while it is still
 * running the action itself; a late real result is kept on the action without changing the operator's FAILED.
 */
export function failStaleAction(id: number): Promise<ActionItem> {
  return hubRequest<{ ok: boolean; action: ActionItem }>(`/api/actions/${id}/fail-stale`, {
    method: 'POST',
    body: JSON.stringify({ confirm_id: id }),
  }).then((r) => r.action);
}

export function rejectAction(id: number): Promise<ActionItem> {
  return hubRequest<{ ok: boolean; action: ActionItem }>(`/api/actions/${id}/reject`, { method: 'POST' }).then(
    (r) => r.action
  );
}

// --- Plans (fleet provisioning) ---------------------------------------------------

export async function listPlans(thread?: string, project?: string): Promise<PlanItem[]> {
  const params = new URLSearchParams();
  if (thread) params.set('thread', thread);
  if (project) params.set('project', project);
  const q = params.toString() ? `?${params.toString()}` : '';
  const r = await hubRequest<{ plans: PlanItem[] | null }>(`/api/plans${q}`);
  return r.plans || [];
}

export async function getPlan(id: string): Promise<PlanItem> {
  const r = await hubRequest<{ plan: PlanItem }>(`/api/plans/${encodeURIComponent(id)}`);
  return r.plan;
}

/** Starts the plan. A plan can run for a long time, so the hub answers 202 once it is running; poll {@link getPlan}. */
export function runPlan(id: string): Promise<PlanRunResponse> {
  return hubRequest<PlanRunResponse>(`/api/plans/${encodeURIComponent(id)}/run`, {
    method: 'POST',
    body: JSON.stringify({ confirm_id: id }),
  });
}

export function rejectPlan(id: string): Promise<PlanItem> {
  return hubRequest<{ ok: boolean; plan: PlanItem }>(`/api/plans/${encodeURIComponent(id)}/reject`, { method: 'POST' }).then(
    (r) => r.plan
  );
}

export function jevRecheck(thread: string): Promise<JevRecheckResponse> {
  return hubRequest<JevRecheckResponse>(
    `/api/threads/${encodeURIComponent(thread)}/jev-recheck`,
    { method: 'POST' },
    330000
  );
}

/**
 * Commit a thread's executed file actions to a new branch of the hub workspace and push it. Synchronous on the
 * server (the project's tests run first): no client-side timeout. A refused precondition is a HubError (409).
 */
export function publishThread(thread: string, branch?: string): Promise<PublishThreadResponse> {
  return hubRequest<PublishThreadResponse>(
    `/api/threads/${encodeURIComponent(thread)}/publish`,
    { method: 'POST', body: JSON.stringify(branch ? { branch } : {}) },
    null
  );
}

/** What publishing would do for a thread (branch, base commit, files, commit subject, remote). A refusal is a HubError (409). */
export async function getPublishPlan(thread: string, branch?: string): Promise<PublishPlan> {
  const q = branch ? `?branch=${encodeURIComponent(branch)}` : '';
  const r = await hubRequest<{ ok: boolean; plan: PublishPlan }>(`/api/threads/${encodeURIComponent(thread)}/publish-plan${q}`);
  return r.plan;
}

/** Close a thread (status CLOSED). Records are kept; a new debate that continues the thread reopens it. */
export function closeThread(thread: string): Promise<CloseThreadResponse> {
  return hubRequest<CloseThreadResponse>(`/api/threads/${encodeURIComponent(thread)}/close`, { method: 'POST' });
}

/** Preview of removing the hub's key from a node's authorized_keys. */
export function fetchRevokeKeyPreview(id: string): Promise<RevokeKeyPreview> {
  return hubRequest<RevokeKeyPreview>(`/api/nodes/${encodeURIComponent(id)}/revoke-key`, {}, 60000);
}

/** Removes the hub's key from the node's authorized_keys over SSH (502 when the SSH step fails). */
export function revokeNodeKey(id: string): Promise<RevokeKeyResult> {
  return hubRequest<RevokeKeyResult>(
    `/api/nodes/${encodeURIComponent(id)}/revoke-key`,
    { method: 'POST', body: JSON.stringify({ confirm_id: id }) },
    60000
  );
}

/** Recent command-audit entries, newest first. */
export async function fetchAudit(limit = 20): Promise<AuditEntry[]> {
  const r = await hubRequest<{ entries: AuditEntry[] | null }>(`/api/audit?limit=${encodeURIComponent(String(limit))}`);
  return r.entries || [];
}

/** Ids of the threads that are CLOSED. */
export async function fetchClosedThreads(): Promise<string[]> {
  const r = await hubRequest<{ threads: string[] | null }>('/api/threads/closed');
  return r.threads || [];
}

/** Publications of a thread, newest first. */
export async function fetchPublications(thread: string): Promise<Publication[]> {
  const r = await hubRequest<{ publications: Publication[] | null }>(`/api/threads/${encodeURIComponent(thread)}/publications`);
  return r.publications || [];
}

export interface FleetNode {
  id: string;
  name: string;
  hostname: string;
  user: string;
  port: number;
  arch: string;
  group: string;
  key: string;
  recommended: boolean;
  description: string;
  tailscale_ip?: string;
  tailscale_tags?: string[];
  tailscale_domain?: string;
  /** Inventory fields node group rules can match on. */
  tier?: string;
  role?: string;
  guardian_role?: string;
  cli_support?: string[];
  /** Roles of configs/package-roles.json; they protect packages from removal on this node. */
  ops_roles?: string[];
  /** Where host, user and port come from: the hub's SSH fleet file, or the servers.json fallback (not migrated). */
  connection?: 'fleet_file' | 'inventory';
  /** Fleet file nodes only: `pinned` (host key stored at registration) or `accept-new` (trust on first use). */
  host_key_mode?: 'pinned' | 'accept-new';
  /** When the hub key was revoked on this node; null when it was not; absent when the hub does not report it. */
  hub_key_revoked_at?: string | null;
}

// --- Mesh join (docs/design/mesh-join.md) -------------------------------------------------

/** GET /api/mesh/join: whether the hub may approve tailnet logins, and the login server a new node uses. */
export interface MeshJoinInfo {
  enabled: boolean;
  /** Why the feature is off or not ready (configuration, reachability, sudo). */
  problem?: string;
  host?: string;
  user?: string;
  /** Tags the hub may give a node. */
  tags?: string[];
  ready?: boolean;
  server_url?: string;
}

export interface MeshJoinRequest {
  /** The registration URL `tailscale up --login-server ...` printed, or its hskey-authreq-... id. */
  auth: string;
  tags: string[];
  /** Optional new node name in headscale. */
  name?: string;
}

/** POST /api/mesh/join: the registered node. */
export interface MeshJoinResult {
  ok: boolean;
  node_id: number;
  name: string;
  ipv4: string;
  ipv6: string;
  tags: string[];
  user: string;
  host: string;
  warning?: string;
}

/** One tailnet node from GET /api/mesh/nodes. */
export interface MeshNode {
  id: number;
  name: string;
  ipv4: string;
  ipv6: string;
  tags: string[];
  user: string;
  online: boolean;
  /** Unix seconds; 0 when headscale has none. */
  last_seen: number;
  /** The Guardian node with this tailnet IPv4. */
  inventory_id?: string;
  /** Why the dashboard may not remove it. */
  protected?: 'hub' | 'control_server' | 'unmanaged_tags';
}

export interface MeshNodeList {
  host: string;
  tags: string[];
  nodes: MeshNode[];
}

export interface MeshRemoveResult {
  ok: boolean;
  id: number;
  name: string;
  ipv4: string;
  inventory_id?: string;
  note?: string;
}

// --- Node registration (docs/design/node-registration.md, section 6) ---------------------

/** GET /api/nodes/registration: what the hub needs before it can register nodes. */
export interface NodeRegistration {
  fleet_file: string;
  known_hosts_file?: string;
  ssh_config_file?: string;
  include_line?: string;
  include_present?: boolean;
  include_effective?: boolean;
  inventory_file?: string;
  inventory_writable?: boolean;
  hub_tailscale_ip?: string;
  fleet_public_key?: string;
  fleet_public_key_note?: string;
  /** Why the hub's public key cannot be shown (set when the hub's address is unknown). */
  fleet_public_key_blocker?: string;
  managed_nodes?: string[];
  /** `Host` patterns of hand-written blocks the agent leaves alone. */
  unmanaged_hosts?: string[];
  /** Plain sentences naming what prevents registering now. */
  blockers: string[];
  can_register: boolean;
}

export interface NodeConnection {
  id: string;
  hostname: string;
  user: string;
  port: number;
}

export interface HostKey {
  type: string;
  /** `SHA256:` fingerprint, sent back verbatim as `host_key_fingerprint`. */
  fingerprint: string;
}

export interface NodeCheckResult {
  ok: boolean;
  id: string;
  hostname: string;
  port: number;
  /** Informational: the id is already registered (expected when re-checking an existing node). */
  id_exists: boolean;
  keys: HostKey[];
}

/** Metadata the dashboard may set; `guardian_role` is fixed to `node` on registration. */
export interface NodeMetadata {
  tier?: string;
  role?: string;
  guardian_role?: string;
  cli_support?: string[];
  ops_roles?: string[];
  tailscale_tags?: string[];
  description?: string;
}

export interface NodeRegisterRequest extends NodeConnection {
  host_key_fingerprint: string;
  metadata?: NodeMetadata;
}

/** PUT body: only changed fields. A metadata value of null removes the key from the inventory entry. */
export interface NodeUpdateRequest {
  hostname?: string;
  user?: string;
  port?: number;
  host_key_fingerprint?: string;
  metadata?: { [K in keyof NodeMetadata]?: NodeMetadata[K] | null };
}

/** Outcome of `ssh <id> uname -m` on the hub. */
/** How the hub classifies an ssh attempt (apps/agent/ssh_classify.go). */
export type Reachability = 'ok' | 'auth_failed' | 'host_key_changed' | 'unreachable' | 'command_failed';

export interface NodeProbe {
  reachability?: Reachability;
  status: 'ok' | 'key_not_installed' | 'unreachable';
  arch?: string;
  /** Passwordless sudo of the remote user ("yes"/"no"); ops cleanup actions on journal, apt and kernels need it. */
  sudo?: 'yes' | 'no';
  /** Last 400 characters of stderr, one line; present when the status is not ok. */
  stderr?: string;
  duration_ms: number;
}

/** Response of register (201) and update (200). `probe` is absent when an update wrote no connection. */
export interface NodeWriteResult {
  ok: boolean;
  node: FleetNode;
  probe?: NodeProbe;
  /** Line to append to the node's ~/.ssh/authorized_keys when the probe says key_not_installed. */
  authorized_key?: string;
  /** Why `authorized_key` is empty (the hub's address is unknown). */
  authorized_key_blocker?: string;
  warnings?: string[];
}

export interface NodeProbeResult {
  ok: boolean;
  id: string;
  probe: NodeProbe;
  authorized_key?: string;
  authorized_key_blocker?: string;
}

export interface NodeRemoveResult {
  ok: boolean;
  id: string;
  removed: { fleet_block: boolean; known_hosts: boolean; inventory: boolean };
}

export interface SystemMetrics {
  timestamp: string;
  hostname: string;
  os: string;
  arch: string;
  /** Seconds since the hub booted; 0 when it could not be read. */
  uptime_sec: number;
  /** Metrics the hub could not read; their fields are 0, not estimates. */
  issues?: string[];
  cpu_usage_pct: number;
  memory_total_bytes: number;
  memory_used_bytes: number;
  memory_pct: number;
  disk_total_bytes: number;
  disk_used_bytes: number;
  disk_pct: number;
  load_avg: [number, number, number];
  top_processes: Array<{
    pid: number;
    user: string;
    cpu: number;
    mem: number;
    command: string;
  }>;
}

export interface SSHConfigSummary {
  config_path: string;
  port: number;
  permit_root_login: string;
  password_authentication: string;
  pubkey_authentication: string;
  max_auth_tries: number;
  x11_forwarding: string;
  allow_tcp_forwarding: string;
  /** Only meaningful when syntax_checked is true (sshd needs root to test a config). */
  is_valid: boolean;
  syntax_checked: boolean;
  syntax_message: string;
  /** "sshd -T" (effective config) or "config files" (read without root). */
  source: string;
  raw_lines_count: number;
}

export interface SecurityRuleCheck {
  rule_id: string;
  title: string;
  severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'PASS' | 'INFO';
  status: 'PASS' | 'FAIL' | 'WARN' | 'INFO';
  current_value: string;
  expected_value: string;
  remediation: string;
}

export interface SecurityReport {
  score: number;
  assessed_at: string;
  rules: SecurityRuleCheck[];
  failed_count: number;
  warn_count: number;
  info_count: number;
  pass_count: number;
}

export interface AuthorizedKey {
  id: string;
  key_type: string;
  public_key: string;
  comment: string;
  fingerprint: string;
  /** Restrictions before the key type, e.g. from="100.64.0.7",no-agent-forwarding. */
  options?: string;
}

/** One node of GET /api/fleet/ssh (docs/design/fleet-ssh-audit.md). */
export type FleetSSHStatus = 'ok' | 'partial' | 'unreachable' | 'error';

export interface FleetSSHNode {
  id: string;
  reachability?: Reachability;
  name: string;
  /** ok: sshd -T; partial: config files read without root, or probe output cut short; no report otherwise. */
  status: FleetSSHStatus;
  /** "sshd -T (sudo)", "sshd -T" or "sshd_config (fallback)". */
  source?: string;
  login_user?: string;
  duration_ms: number;
  /** Absent when nothing could be evaluated: such nodes are never scored on defaults. */
  config?: SSHConfigSummary;
  report?: SecurityReport;
  /** The login user's authorized_keys; public_key is left empty (the fingerprint identifies the key). */
  keys: AuthorizedKey[];
  notes: string[];
  error?: string;
}

export interface FleetSSHRuleTally {
  fail: number;
  warn: number;
  pass: number;
  info: number;
}

export interface FleetSSHSummary {
  nodes: number;
  audited: number;
  unreachable: number;
  errors: number;
  /** Average over audited nodes; null when none was audited. */
  avg_score: number | null;
  failing_nodes: string[];
  /** Rule catalog in evaluation order. */
  rules: Array<{ rule_id: string; title: string }>;
  by_rule: Record<string, FleetSSHRuleTally>;
}

export interface FleetSSHReport {
  generated_at: string;
  duration_ms: number;
  /** Served from the hub's 60s cache. */
  cached: boolean;
  nodes: FleetSSHNode[];
  summary: FleetSSHSummary;
}

export interface RemediationAction {
  id: string;
  title: string;
  command: string;
  riskLevel: 'LOW' | 'MEDIUM' | 'HIGH';
  requiresApproval: boolean;
  warning?: string;
}

export interface FleetNodeResult {
  id: string;
  name: string;
  group: string;
  arch: string;
  status: 'ONLINE' | 'OFFLINE';
  reachability?: Reachability;
  os_arch?: string;
  uptime: string;
  load_avg?: string;
  memory?: string;
  disk?: string;
  duration_ms: number;
}

export interface FleetDiagnosisReport {
  total_nodes: number;
  online_count: number;
  duration_sec: number;
  results: FleetNodeResult[];
}

export interface FleetExecResult {
  id: string;
  name: string;
  group: string;
  arch: string;
  exit_code: number;
  stdout: string;
  stderr: string;
  duration_ms: number;
  reachability?: Reachability;
}

export interface FleetExecReport {
  command: string;
  total_nodes: number;
  success_count: number;
  failed_count: number;
  duration_sec: number;
  results: FleetExecResult[];
}

export interface FleetStreamEvent {
  type: 'start' | 'node_start' | 'node_done' | 'complete' | 'error';
  command?: string;
  total?: number;
  completed?: number;
  success?: number;
  failed?: number;
  elapsed_sec?: number;
  node?: FleetNode;
  result?: FleetExecResult;
  message?: string;
}

export type BatchExecutionStatus = 'IDLE' | 'RUNNING' | 'COMPLETED' | 'CANCELLED';

export interface BatchNodeState {
  node: FleetNode;
  status: 'WAITING' | 'RUNNING' | 'SUCCESS' | 'FAILED';
  exitCode?: number;
  stdout?: string;
  stderr?: string;
  durationMs?: number;
}

/** Seats a character (format 2) may take (agent-characters.md 2.1): the debate seats plus `solo` for the solo runner. */
export type CharacterSeat = 'expert' | 'proposer' | 'critic' | 'verifier' | 'judge' | 'stage' | 'side' | 'solo';

/** The `persona` block of a character file: the only part sent to the model, rendered by one fixed template. */
export interface CharacterBlock {
  /** One noun phrase; completes "You are ...". */
  identity: string;
  /** 1-4 noun phrases. */
  expertise: string[];
  /** What the character optimises and what it trades away. */
  stance: string;
  /** Things to look at first, not output instructions. */
  checks: string[];
  /** What it leaves to others; may be empty or absent. */
  boundaries?: string[];
}

export interface PersonaConfig {
  id: string;
  name: string;
  /** Format 1 only: free-text role label. A character declares its seats in `seats` instead. */
  role?: string;
  /** Format 1 only: a character carries its definition field by field in `persona`. */
  domain?: string;
  system_prompt?: string;
  focus_areas?: string[];
  critique_focus?: string[];
  mcp_capabilities?: string[];
  /** The persona's home seat when it opted into the engine's role cards (agent-characters.md, C1). */
  role_card?: 'expert' | 'critic' | 'judge';
  /** The kind of role cards it gets with role_card (absent: the code cards). */
  role_card_kind?: 'ops';
  /** Shown to people only, never sent to an agent (docs/design/agent-characters.md). */
  profile?: PersonaProfile;
  /** `2` marks a character file (agent-characters.md, phase C2); absent is the format 1 free-text persona. */
  format?: 2;
  /** Format 2: the seats this character may take (replaces the free-text `role`). */
  seats?: CharacterSeat[];
  /** Format 2: project kinds the character is written for; empty means any. */
  kinds?: ('code' | 'ops' | 'design')[];
  /** Format 2: the character's field-by-field definition, the only part sent to the model. */
  persona?: CharacterBlock;
  /** Format 2: ceiling of tools the character may propose (the judge seat; today's `mcp_capabilities`). */
  tools?: string[];
  /** Format 2: the persona text the engine sends, computed by the hub; read-only, never sent back on save. */
  rendered?: string;
  source?: 'builtin' | 'custom';
}

export interface PersonaProfile {
  tagline?: string;
  look?: string;
}

export type SuggestSeat = 'expert' | 'proposer' | 'critic' | 'judge';
export type SuggestKind = 'code' | 'ops' | 'design';

/** POST /api/personas/suggest: one model turn drafts a persona and its profile; nothing is saved. */
export interface PersonaSuggestRequest {
  brief: string;
  seat: SuggestSeat;
  kind: SuggestKind;
  provider?: string;
  current?: Partial<PersonaConfig>;
}

export interface PersonaSuggestResult {
  suggestion: PersonaConfig;
  warnings: string[];
  provider: string;
  examples: string[];
}

export interface PersonaList {
  personas: PersonaConfig[];
  custom_dir?: string;
}

export interface SaveResult {
  id: string;
  warnings: string[];
}

// --- Command sets (docs/design/batch-execution.md, B4c) ----------------------

export interface CommandSetParam {
  type: 'int' | 'enum' | 'unit' | 'package' | 'path' | 'string';
  min?: number;
  max?: number;
  values?: string[];
  default?: string | number;
  pattern?: string;
}

/** A stored command set. `reviewed` is stamped by the hub when it saves; `source` says where the file lives. */
export interface CommandSet {
  id: string;
  description: string;
  source?: 'builtin' | 'operator';
  os: string[];
  needs_sudo: boolean;
  check_sudo?: boolean;
  params: Record<string, CommandSetParam>;
  check: string;
  script: string;
  reversible: boolean;
  undo: string;
  risk: 'low' | 'medium' | 'high';
  timeout: number;
  reviewed?: string;
  /** Whether the batch gate may release plans using this set without a person: builtin sets always, the operator's own
   * only after they confirmed this exact text. Set by the hub; never sent when saving. */
  confirmed?: boolean;
}

export interface CommandSetList {
  ok: boolean;
  max_age_days: number;
  stale: { id: string; reviewed: string; age_days: number }[];
  sets: CommandSet[];
}

// --- Debate teams -----------------------------------------------------------

/** How a member reads repository code: `mcp` = MCP tools (claude, codex or opencode); absent = engine-mediated repo blocks. */
export type CodeTools = 'mcp';

export interface TeamMemberData {
  persona: string;
  providers?: string[];
  code_tools?: CodeTools | null;
  /** Where the member runs: a placement label or a worker host id; absent means the hub. */
  host?: string;
  /** Keys the editor does not model (`host`, ...): kept as stored. */
  [key: string]: unknown;
}

// A member in a team file may be a plain persona id string.
export type TeamMemberRaw = string | TeamMemberData;

export interface TeamContext {
  paths: string[];
  ref?: string;
}

// Team file as stored (editor initial value); critic may be a single member or a list.
export interface TeamFileData {
  id: string;
  name?: string;
  expert?: TeamMemberRaw;
  proposers?: TeamMemberRaw[];
  critic?: TeamMemberRaw | TeamMemberRaw[];
  judge?: TeamMemberRaw;
  max_rounds?: number;
  allowed_tools?: string[];
  context?: TeamContext;
  /** List metadata: goal category (fixed list from the catalog); the hub stamps `created` and `modified`. */
  category?: string;
  created?: string;
  modified?: string;
  /** Keys the editor does not model (`flow`, `sides`, ...): kept as stored. */
  [key: string]: unknown;
}

// Normalized form used by the editor and sent on save.
export interface TeamData {
  id: string;
  name: string;
  expert: TeamMemberData & { providers: string[] };
  proposers?: Array<TeamMemberData & { providers: string[] }>;
  critic: Array<TeamMemberData & { providers: string[] }>;
  judge: TeamMemberData & { providers: string[] };
  max_rounds?: number;
  allowed_tools?: string[];
  context?: TeamContext;
  category?: string;
  /** Keys the editor does not model (`flow`, `sides`, ...): written back as stored. */
  [key: string]: unknown;
}

export interface TeamEntry {
  id: string;
  name: string;
  source: 'builtin' | 'custom';
  data: TeamFileData;
  lineup?: unknown;
  errors: string[];
  warnings: string[];
}

export interface ToolInfo {
  name: string;
  risk: string;
  description: string;
}

export interface TeamCatalog {
  providers: string[];
  installed: string[];
  tools: ToolInfo[];
  max_critics: number;
  max_proposers?: number;
  max_rounds: number;
  categories?: string[];
  /** Placement labels a member's `host` may hold (`worker:any`, `worker:other`). */
  host_labels?: string[];
  /** Enabled worker hosts (inventory ids) a member's `host` may name. */
  workers?: string[];
}

export interface TeamList {
  custom_dir?: string;
  teams: TeamEntry[];
  catalog: TeamCatalog;
}

// --- Flow templates ---------------------------------------------------------

export type FlowParamType = 'string' | 'text' | 'int' | 'enum';

export interface FlowParamSpec {
  type: FlowParamType;
  label?: string;
  max?: number;
  min?: number;
  values?: string[];
  default?: string | number;
}

/** One step of a template: an alias of a flow kind (T1) or a custom step (T3a: `draft`, `blind_draft`, `critique` + `mode`). */
export interface FlowStep {
  kind: string;
  mode?: string;
}

/** A template file as stored (docs/design/flow-templates.md): one alias step of a flow kind, or custom steps. */
export interface FlowTemplateData {
  id: string;
  name: string;
  category?: string;
  projects: Array<'code' | 'ops' | 'design'>;
  team?: string;
  steps: FlowStep[];
  params?: Record<string, FlowParamSpec>;
  task: string;
  stances?: Record<string, string>;
  max_rounds?: number;
  reviewed?: string;
  [key: string]: unknown;
}

export interface FlowTemplateEntry {
  id: string;
  name: string;
  source: 'builtin' | 'custom';
  data: FlowTemplateData;
  /** `{kind, hash, team}` once the file validates; null while it has errors. */
  resolved: { kind: string; hash: string; team?: string } | null;
  errors: string[];
  warnings: string[];
}

export interface FlowTemplateList {
  custom_dir?: string;
  templates: FlowTemplateEntry[];
  catalog: FlowTemplateCatalog;
}

export interface FlowTemplateCatalog {
  /** The flow kinds a one-step alias may name. */
  step_kinds: string[];
  /** The custom steps of T3a; absent on a hub from before T3a. */
  custom_step_kinds?: string[];
  critique_modes?: string[];
  limits?: { steps?: number; params?: number; task_chars?: number; stance_chars?: number; max_rounds?: number };
  param_types: string[];
  project_kinds: string[];
  categories: string[];
}

// --- Flow graphs (docs/design/flow-graph.md) ---------------------------------

export type FlowJob = 'write' | 'critique' | 'rewrite' | 'rebut' | 'summarise';
export type FlowInputMode = 'raw' | 'condensed' | 'structured' | 'latest';

/** A seat (자리): one character doing one job. A summariser has no character. */
export interface FlowSeat {
  id: string;
  job: FlowJob;
  character?: string;
  providers?: string[];
  code_tools?: string | null;
  host?: string | null;
  /** critique only: `object` (objections) or `checklist`. */
  mode?: string;
  /** rebut only: the position this side defends (a person's sentence or ${PARAM}). */
  stance?: string;
  input?: FlowInputMode;
  history?: string;
  /** summarise only: an instruction that replaces the generated one. */
  instruction?: string;
  focus?: string;
}

/** A bundle (동시 묶음): seats that run in parallel. */
export interface FlowBundle {
  id: string;
  seats: string[];
  /** A bundle of two rebut seats: how often the sides answer each other. */
  exchanges?: number;
}

/** The close (마감): the Judge and the shared close. */
export interface FlowClose {
  character: string;
  providers?: string[];
  code_tools?: string | null;
  host?: string | null;
  allowed_tools?: string[];
  input?: FlowInputMode;
  history?: string;
}

/** A link (연결): `to` reads `from`; a look-back link reads the previous round; the close has two exits. */
export interface FlowLink {
  from: string;
  to: string;
  look_back?: boolean;
  blocked?: boolean;
  exit?: 'approved' | 'rejected';
  max_rounds?: number;
}

export interface FlowData {
  id: string;
  name: string;
  category?: string;
  projects?: Array<'code' | 'ops' | 'design'>;
  params?: Record<string, FlowParamSpec>;
  task?: string;
  seats: FlowSeat[];
  bundles?: FlowBundle[];
  close: FlowClose;
  links: FlowLink[];
  /** Canvas positions only; never changes how the flow runs. */
  layout?: Record<string, [number, number]>;
  context?: { paths: string[]; ref?: string };
  converted_from?: { team?: string; template?: string };
  [key: string]: unknown;
}

export interface FlowEntry {
  id: string;
  name: string;
  /** Stored flows are builtin or custom; teams and templates are listed converted. */
  source: 'builtin' | 'custom' | 'team' | 'template';
  data: FlowData;
  hash: string;
  /** Model calls of a first round; null while the flow has errors. */
  calls: number | null;
  /** Whether the engine runs this shape yet; run_error says why not. */
  runnable: boolean;
  run_error: string | null;
  errors: string[];
  warnings: string[];
}

export interface FlowCatalog {
  jobs: FlowJob[];
  critique_modes: string[];
  input_modes: FlowInputMode[];
  history_modes: string[];
  limits: { seats: number; bundle_seats: number; calls_per_round: number; max_rounds: number; exchanges: number; params: number; task_chars: number; stance_chars: number; focus_chars: number };
  categories: string[];
  project_kinds: string[];
  param_types: string[];
}

export interface FlowList {
  custom_dir?: string;
  flows: FlowEntry[];
  catalog: FlowCatalog;
}

// --- Debate jobs ------------------------------------------------------------

export type DebateJobStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';

export interface DebateJob {
  /** Absent on jobs started before projects existed (the default project). */
  project?: string;
  id: string;
  task: string;
  team: string;
  thread_id: string;
  rounds: number;
  status: DebateJobStatus;
  started_at: string;
  /** `null` while the job runs (the hub sends a null time). */
  finished_at: string | null;
  exit_code: number | null;
  error: string;
  context_paths?: string[];
  context_ref?: string;
  /** Flow template the job was started from, with the parameter values; `task` is then the rendered text. */
  template?: string;
  params?: Record<string, string>;
  /** Flow the job was started from (docs/design/flow-graph.md) and the characters swapped into its seats. */
  flow?: string;
  seats?: Record<string, string>;
  /** List responses only: whether the job wrote an event file (the Agents monitor has data for it). */
  events?: boolean;
  /** The job's thread was closed (status CLOSED): records stay, default lists hide it, a new debate reopens it. */
  thread_closed?: boolean;
  /** List responses: the thread was published as a branch; the hub refuses new debates on it. */
  thread_published?: boolean;
  /** Retrieval cost of the run; absent for runs that predate it. */
  retrieval?: { results: number; bytes: number };
  /** `code_wiki` for a code wiki build job (the Agents monitor follows it like a debate); absent for debates. */
  kind?: 'code_wiki';
}

export interface DebateJobDetail extends DebateJob {
  log: string;
  result: DebateResult | null;
}

export interface DebateStartRequest {
  /** Project id (configs/projects); omitted means the hub default (server-guardian). */
  project?: string;
  /** The task text; absent when a flow `template` supplies it. */
  task?: string;
  /** Flow template id (docs/design/flow-templates.md): starts a new thread, `params` fill its placeholders. */
  template?: string;
  params?: Record<string, string>;
  /** Flow id (docs/design/flow-graph.md): seats its own characters and starts a new thread; `params` fill its text. */
  flow?: string;
  /** Characters swapped into the flow's seats (seat id or `close` -> character id). */
  seats?: Record<string, string>;
  team?: string;
  thread_id?: string;
  rounds?: number;
  context_paths?: string[];
  context_ref?: string;
}

export interface DebateTranscriptItem {
  round: number;
  agent: string;
  role: string;
  content: string;
  consensus?: boolean;
  provider?: string;
  /** Why earlier providers were skipped before `provider` answered. */
  fallbacks?: string[];
  /** The flow seat that wrote this entry (a run started from a flow; `close` for the Judge). */
  seat?: string;
}

export interface DebateAction {
  id: number | string;
  tool: string;
  status: string;
  error?: string;
  /** Requirement ids (R1, ...) this action implements. */
  refs?: string[];
  /** The file the action writes (file actions only). */
  file_path?: string;
}

export interface DebateFinalDecision {
  approved?: boolean;
  unverified?: boolean;
  /** Why the round was unverified (the LLM provider error). */
  llm_error?: string | null;
  jev_gate?: string;
  final_round?: number;
  judgment?: string;
  /** The judge's final approach (sent to jev as the approach under review). */
  decision?: string;
  /** Things to confirm in the code after the judge's actions run (not sent to jev). */
  checks?: string[];
  /** Why this approach (numbered items, line breaks kept). */
  rationale?: string;
  constraints?: string[];
  /** Requirement ids per constraint (parallel to `constraints`). */
  constraint_refs?: string[][];
  /** Requirement ids per check (parallel to `checks`). */
  check_refs?: string[][];
  proposal?: string;
  critique?: string;
  jev?: { reasons?: string[] };
  actions?: DebateAction[];
}

export interface DebateContextFile {
  path: string;
  bytes: number;
  truncated: boolean;
  /** Selectors that picked parts of the file ("#executeSSH", "lines 110-145"); absent for whole files. */
  parts?: string[];
  /** Whether an outline of the file was included with the parts. */
  outline?: boolean;
}

export interface DebateContextSkipped {
  path: string;
  reason: string;
}

export interface DebateContext {
  ref: string;
  sha: string;
  paths: string[];
  files: DebateContextFile[];
  skipped: DebateContextSkipped[];
  total_bytes: number;
  tree_files: number;
}

export interface DebateResult {
  context?: DebateContext | null;
  thread_id?: string;
  /** The lineup snapshot; `flow_graph` when the run started from a flow (id, hash, the characters seated). */
  team?: string | { id?: string; name?: string; flow_graph?: { id: string; hash: string; seats?: Record<string, string | null> } } | null;
  task?: string;
  consensus_reached?: boolean;
  final_decision?: DebateFinalDecision | null;
  transcript: DebateTranscriptItem[];
  log_file?: string;
}

// --- Code wiki (docs/design/debate-code-index-wiki.md) -------------------------------

export type WikiState = 'fresh' | 'stale' | 'broken';

/** `{kind: 'llm', provider, job}` for a written page, `{kind: 'human', user}` for an edited one. */
export interface WikiAuthor {
  kind?: string;
  provider?: string;
  job?: string;
  user?: string;
}

export interface WikiPageSummary {
  id: string;
  title: string;
  scope: string[];
  author: WikiAuthor;
  updated: string;
  state: WikiState;
  /** dir, overview (a split directory), group (a part of one) or feature (a design document); absent from older hubs. */
  kind?: WikiKind;
  /** First paragraph of the page, citations left out, at most 160 characters; absent from older hubs. */
  summary?: string;
  /** Ids of the other pages the narrative links to as `[[id]]`, in order; absent from older hubs. */
  links?: string[];
}

export type WikiKind = 'dir' | 'overview' | 'group' | 'feature';

export interface WikiList {
  project: string;
  sha: string;
  pages: WikiPageSummary[];
  /** Pages the hub could not read (`{id, error}`). */
  errors: Array<{ id: string; error: string }>;
}

export interface WikiStaleness {
  state: WikiState;
  sha: string;
  written_sha: string;
  changed: string[];
  removed: string[];
  new: string[];
  broken: Array<{ citation: string; reason: string }>;
  unverified: string[];
  notes: string[];
}

export interface WikiPageMeta {
  id: string;
  title: string;
  scope: string[];
  author: WikiAuthor;
  updated: string;
  written_sha?: string;
  prompt_version?: string;
  basis?: Record<string, string>;
  unverified?: string[];
  kind?: WikiKind;
  /** Other documents a feature page's design document names. */
  docs?: string[];
}

export interface WikiPageDetail {
  project: string;
  sha: string;
  page: WikiPageMeta;
  /** The written layer (Markdown source); the generated layer is not part of it. */
  narrative: string;
  state: WikiState;
  staleness: WikiStaleness;
  /** The generated layer: the scope's repository map at `sha`, made when the page is opened. */
  generated: string;
}

/** Body of `PUT /api/projects/{id}/wiki/{page}`; the hub fills the id from the path. */
export interface WikiSaveRequest {
  title: string;
  scope: string[];
  body: string;
}

export interface WikiSaveResult {
  page: WikiPageMeta;
  state: WikiState;
  staleness: WikiStaleness;
}

/** Body of `POST /api/projects/{id}/wiki/build`: one of pages / stale / all, or none for the default page set. */
export interface WikiBuildRequest {
  pages?: string[];
  stale?: boolean;
  all?: boolean;
  /** Also overwrite pages a person wrote. */
  force?: boolean;
  /** Only pages of this kind; alone, every candidate page of it (feature: one per design document). */
  kind?: WikiKind;
}

// --- Code files (file view of the wiki, docs/design/code-wiki-coverage.md C) -------

/** GET /api/projects/{id}/files: the readable files of the commit, by path. */
export interface CodeFileList {
  project: string;
  sha: string;
  files: Array<{ path: string; size: number }>;
  truncated: boolean;
}

/** A file that uses this one (incoming) or that this one uses (outgoing); name based, import checked. */
export interface CodeFileRef {
  path: string;
  /** Reference lines. */
  count: number;
  /** A test, story or preview file. */
  test: boolean;
}

export interface CodeFileDecl {
  name: string;
  qualified: string;
  kind: string;
  start: number;
  end: number;
  /** First line of the declaration, string literals masked. */
  signature: string;
}

/** GET /api/projects/{id}/files/show?path=: one file without an LLM. */
export interface CodeFileDetail {
  project: string;
  sha: string;
  path: string;
  language: string | null;
  size: number;
  binary: boolean;
  lines: number;
  indexed: boolean;
  outline: CodeFileDecl[];
  incoming: CodeFileRef[];
  outgoing: CodeFileRef[];
  /** How references are counted. */
  references: string;
  pages: Array<{ id: string; title: string; state: WikiState; via: 'scope' | 'basis' | 'cited' }>;
  docs: Array<{ path: string; title: string; lines: number[] }>;
}

/** GET /api/projects/{id}/files/lines?path=&start=&end=: source lines (audited on the hub). */
export interface CodeFileLines {
  project: string;
  sha: string;
  path: string;
  start: number;
  end: number;
  total: number;
  lines: string[];
  truncated: boolean;
}

// --- Debate events (Agents monitor) ----------------------------------------------
// One JSON object per line of the job's events.jsonl; see docs/design/debate-agent-retrieval.md.

/** Retrieval cost reported by `debate_finished`. */
export interface DebateRetrievalSummary {
  results: number;
  bytes: number;
  by_agent?: Record<string, { results: number; bytes: number; steps: number }>;
}

/** Fields every event carries. `round`, `role`, `agent` and `agent_name` appear on agent events only. */
export interface DebateEventBase {
  /** Increasing from 1; the cursor for `after=`. */
  seq: number;
  ts: string;
  thread_id?: string;
  round?: number;
  role?: string;
  /** Session key (`lineup.session_key`); tells panel critics apart. */
  agent?: string;
  agent_name?: string;
}

export interface DebateEventMember {
  role: string;
  agent: string;
  agent_name?: string;
  providers?: string[];
}

/** `mcp` = served by MCP tools; `block` (also the default for older runs) = engine-mediated repo blocks. */
/** block: ```repo blocks; mcp: repo MCP tools; ops: read-only node checks of an ops project. */
export type RetrievalVia = 'mcp' | 'block' | 'ops';

export interface RetrievalRequest {
  op: string;
  target: string;
}

export interface RetrievalResult {
  op: string;
  target: string;
  ok: boolean;
  bytes: number;
  truncated?: boolean;
  /** `excluded` for secret / binary / symlink paths, otherwise a short message. */
  error?: string;
  /** Held back (over the per-step limit or budget); the agent should ask again next step. `error` is `deferred`. */
  deferred?: boolean;
}

/** One checked item of `evidence_check`: a findings-block entry compared with the lines the agent was shown. */
export interface EvidenceItem {
  path?: string;
  line?: number | string | null;
  ok?: boolean;
  /** `verified` or why the item does not match. */
  reason?: string;
}

/** `compiler_index` (Go compiler-assisted index): `status` is built, cached, failed, timeout or unavailable. */
export interface CompilerIndexData {
  lang?: string;
  sha?: string;
  status?: string;
  packages?: number;
  defs?: number;
  refs?: number;
  unresolved?: number;
  ms?: number;
  error?: string | null;
}

export type DebateEvent = DebateEventBase &
  (
    | {
        type: 'debate_started';
        data: {
          task?: string;
          project?: string;
          team?: string;
          max_rounds?: number;
          /** Host that runs the engine and every agent's LLM CLI. */
          host?: string;
          members?: DebateEventMember[];
          context?: { sha?: string; files?: number };
        };
      }
    | { type: 'round_started'; data?: Record<string, never> }
    | { type: 'agent_started'; data?: { resumed?: boolean; prompt_bytes?: number; prompt_saved?: boolean; prompt_truncated?: boolean } }
    | { type: 'provider_failed'; data?: { provider?: string; error?: string } }
    | { type: 'retrieval_requested'; data?: { step?: number; via?: RetrievalVia; requests?: RetrievalRequest[] } }
    | { type: 'retrieval_served'; data?: { step?: number; via?: RetrievalVia; results?: RetrievalResult[]; turn_bytes?: number } }
    | { type: 'action_repair'; data?: { invalid?: Array<{ index?: number; tool?: string; error?: string }>; fixed?: number } }
    | { type: 'format_retry'; data?: { missing?: Array<'verdict' | 'json'> } }
    | { type: 'pseudo_tool_call'; data?: { step?: number; tools?: string[]; discarded_chars?: number } }
    | { type: 'reply_trimmed'; data?: { step?: number; discarded_chars?: number; blocks?: number } }
    | { type: 'citation_check'; data?: { cited?: number; unverified?: string[]; asked?: boolean } }
    | { type: 'retrieval_exhausted'; data?: { steps?: number; turn_bytes?: number } }
    | {
        type: 'agent_finished';
        data?: {
          provider?: string;
          duration_ms?: number;
          steps?: number;
          output_bytes?: number;
          excerpt?: string;
          /** The built-in canned fallback answered instead of a real provider. */
          fallback?: boolean;
        };
      }
    | { type: 'verdict'; data?: { approved?: boolean; unverified?: boolean } }
    | { type: 'jev_gate'; data?: { gate?: string; reasons?: string[] } }
    | { type: 'action_proposed'; data?: { id?: number | string; tool?: string; status?: string } }
    | { type: 'action_executed'; data?: { id?: number | string; tool?: string; status?: string } }
    | { type: 'retrieval_warning'; data?: { step?: number; bytes?: number; threshold?: number; via?: RetrievalVia } }
    | { type: 'evidence_check'; data?: { checked?: number; mismatched?: number; items?: EvidenceItem[] } }
    | { type: 'compiler_index'; data?: CompilerIndexData }
    | { type: 'compiler_index_warning'; data?: { lang?: string; sha?: string; message?: string } }
    | { type: 'wiki_build_started'; data?: { project?: string; team?: string; sha?: string; pages?: string[]; language?: string; force?: boolean; host?: string } }
    | { type: 'wiki_page_written'; data?: { page?: string; state?: string; basis_files?: number; citations?: number; unverified?: string[] } }
    | { type: 'wiki_page_skipped'; data?: { page?: string; reason?: string } }
    | { type: 'wiki_build_finished'; data?: { written?: number; skipped?: number; failed?: number } }
    | { type: 'debate_finished'; data?: { status?: string; consensus?: boolean; final_round?: number; retrieval?: DebateRetrievalSummary } }
    | { type: 'error'; data?: { message?: string } }
    | { type: 'events_truncated'; data?: Record<string, never> }
  );

export type DebateEventType = DebateEvent['type'];

/** Response of `GET /api/debates/{id}/prompts/{seq}`: the turn prompt saved for one agent_started event. */
export interface DebatePrompt {
  seq: number;
  bytes: number;
  text: string;
}

/** One diagram file stored next to a thread report (`<thread>.assets/<name>`). */
export interface ThreadReportAsset {
  name: string;
  bytes: number;
}

/** Response of `GET /api/threads/{thread}/report`: the Markdown result document of a debate thread. */
export interface ThreadReport {
  thread_id: string;
  project: string;
  markdown: string;
  bytes: number;
  updated_at: string;
  assets: ThreadReportAsset[];
}

/** Response of `GET /api/threads/{thread}/report/assets/{name}`: an SVG as JSON text (an <img> cannot carry the auth header). */
export interface ThreadReportAssetFile {
  name: string;
  bytes: number;
  text: string;
}

/** Response of `GET /api/debates/{id}/events?after=N`. */
export interface DebateEventsPage {
  events: DebateEvent[];
  /** Last seq in `events`, or the `after` that was asked for when there were none. */
  next: number;
  /** The job has ended and no events remain after `next`. */
  done: boolean;
}

/** Lines the follow stream sends besides events. */
export type DebateStreamControl = { type: 'heartbeat' } | { type: 'end'; status?: string };
export type DebateStreamMessage = DebateEvent | DebateStreamControl;

// --- Judge action approval ---------------------------------------------------

/** STALE: an ops cleanup action whose target grew too much since it was proposed; it never runs. */
export type ActionStatus = 'PROPOSED' | 'HELD' | 'RUNNING' | 'EXECUTED' | 'FAILED' | 'REJECTED' | 'INVALID' | 'STALE' | 'SKIPPED';

export interface ActionItem {
  /** Project whose workspace this action runs in. */
  project_id?: string;
  id: number;
  thread_id: string;
  round_num?: number;
  tool: string;
  arguments?: Record<string, unknown> | null;
  reason?: string;
  status: ActionStatus | string;
  result?: unknown;
  decided_by?: string | null;
  created_at?: string;
  updated_at?: string;
  risk?: string;
  tool_description?: string;
  base_sha?: string | null;
  diff?: string;
  /** ops cleanup actions: what the action would delete when it was proposed (bytes null when unreadable). */
  preview?: ActionPreview | null;
  /** Set on a step of a plan (fleet provisioning): the step runs only through its plan. */
  plan_id?: string | null;
  step?: string | null;
  /** Plan steps: whether the step can be undone (an irreversible step always waits for a person). */
  reversible?: boolean;
  requires?: string[];
  on_failure?: 'stop' | 'continue' | string | null;
}

/** One critic's stored vote on a plan (docs/design/batch-execution.md, chapter 6). */
export interface PlanVote {
  critic: string;
  vote: 'approve' | 'oppose' | 'abstain';
  reason: string;
  /** Verified node-state checks the critic cited. */
  cites: string[];
  /** An objection with a verified check; an uncounted oppose is advice only. */
  counted: boolean;
  note?: string;
}

/** What the batch gate made of the votes, and what the engine did with it (B2/B3). */
export interface PlanGate {
  /** Released for an automatic run (the project opted in and every condition held). */
  auto: boolean;
  /** What the gate would have decided had the project opted in. */
  would_auto: boolean;
  reasons: string[];
  notes: string[];
  tally: Record<string, number>;
  plan_hash?: string;
  at?: string;
  auto_run?: { actor?: string; started_at?: string; finished_at?: string; counts?: Record<string, number>; error?: string };
  release_refused?: { reasons: string[]; at?: string };
}

/** One plan of ordered package steps over several nodes (docs/design/fleet-provisioning.md, P3). */
export interface PlanItem {
  /** Critics' votes and the gate decision; absent on plans that went through no vote pass. */
  votes?: PlanVote[] | null;
  gate?: PlanGate | null;
  plan_hash?: string | null;
  id: string;
  thread_id: string;
  round_num?: number;
  project_id?: string | null;
  goal?: string | null;
  /** A failed step stops the rest of the plan (steps that did not start become SKIPPED). */
  stop_plan: boolean;
  created_at?: string;
  steps: ActionItem[];
  hosts: Array<{ host: string; steps: string[] }>;
  /** Steps per status. */
  counts: Record<string, number>;
  /** Highest risk among the steps. */
  risk: string;
  /** Some step waits for a decision and none is HELD. */
  runnable: boolean;
}

/** POST /api/plans/{id}/run: 202 {started} while the plan runs, or the finished outcome when it ended within the start grace. */
export interface PlanRunResponse {
  started?: boolean;
  plan_id?: string;
  ok?: boolean;
  plan?: PlanItem;
}

export interface ActionPreview {
  bytes: number | null;
  text?: string;
  error?: string | null;
  /** Package actions: "pattern (source)" lines of what is protected on the node. */
  protected?: string[];
  /** Package actions: why the derived protection of the node may be incomplete (unknown owners of listening ports). */
  blind?: string[];
}

export interface ActionCheckResult {
  cmd: string;
  cwd?: string;
  exit_code: number;
  stdout?: string;
  stderr?: string;
}

export interface ActionRunResponse {
  ok: boolean;
  action: ActionItem;
  result?: Record<string, unknown> | null;
}

export interface JevRecheckResponse {
  ok: boolean;
  gate: string;
  released?: Array<number | string>;
  review?: { reasons?: string[]; [k: string]: unknown } | null;
}

/** Outcome of publishing a thread's executed file actions (POST /api/threads/{thread}/publish). */
export interface PublishThreadResponse {
  ok: boolean;
  /** PUSHED, or FAILED: the commit stays on a local branch of the hub workspace and stderr says why. */
  status: string;
  thread_id?: string;
  branch?: string;
  commit_sha?: string | null;
  remote?: string;
  files?: string[];
  error?: string;
  stderr?: string;
}

/** What a publish would do, from GET /api/threads/{thread}/publish-plan. */
export interface PublishPlan {
  thread_id: string;
  project_id: string;
  branch: string;
  remote: string;
  base_sha: string;
  files: string[];
  /** Commit subject. */
  subject: string;
  /** The branch exists already from an earlier push attempt, so this publish retries it. */
  retry: boolean;
}

export interface CloseThreadResponse {
  ok: boolean;
  thread_id: string;
  status: string;
  previous_status?: string;
  /** True when the thread was already CLOSED (closing is idempotent). */
  already_closed?: boolean;
  closed_at?: string | null;
}

// --- Projects (repositories the debate engine reads and changes) -----------------

export interface ProjectTestCommand {
  prefix: string;
  /** flag -> whether it takes a value */
  flags?: Record<string, boolean>;
}

export interface ProjectData {
  id: string;
  name?: string;
  description?: string;
  /** https clone URL; optional when workspace points at an existing clone */
  repo?: string;
  branch?: string;
  /** ~/... path on the hub; default ~/guardian-workspaces/<id> */
  workspace?: string;
  tests?: { default?: string; commands?: ProjectTestCommand[] };
  context?: { paths: string[]; ref?: string };
  /**
   * 'ops': a set of fleet nodes without a repository (docs/design/server-ops-project.md); 'design': documents and
   * diagrams without a repository (docs/design/design-project.md); absent means 'code'.
   */
  kind?: 'code' | 'ops' | 'design';
  /** Default flow (or team) of debates started without one (D7 of docs/design/flow-graph.md); the key keeps its old name. */
  team?: string;
  /** ops projects: node groups and nodes the actions may target (the hub never, unless policy.include_hub). */
  targets?: { all?: boolean; groups?: string[]; nodes?: string[]; exclude?: string[] };
  policy?: {
    allowed_actions?: string[];
    include_hub?: boolean;
    /** Absolute fnmatch patterns that cleanup actions may never touch (the hub's defaults always apply). */
    deny_paths?: string[];
    /** Command patterns (fnmatch on each command word and line) that an ad-hoc script may never contain. */
    deny_commands?: string[];
    /** Package name patterns (fnmatch) that package actions may never remove, on top of the roles of each node. */
    protected_packages?: string[];
    /** Low-risk catalog actions the engine runs without a person once jev passed the decision. */
    auto_run?: string[];
    max_auto_runs_per_day?: number;
    /** Batch plans (docs/design/batch-execution.md): nodes at once, steps per plan and whether released plans run without a person. */
    batch?: {
      auto?: boolean;
      max_plans_per_day?: number;
      max_steps?: number;
      max_parallel_hosts?: number;
      /** 'vote': ad-hoc scripts on script_groups run on the critics' vote, with no person (needs auto). Default 'approve'. */
      scripts?: 'approve' | 'vote';
      script_groups?: string[];
    };
    /** Nightly watch settings; the editor does not edit them but must send them back unchanged. */
    watch?: unknown;
  };
}

/** Resolved nodes of an ops project, as the project list reports them. */
export interface ProjectTargetsSummary {
  ids: string[];
  hub: string | null;
  include_hub: boolean;
  missing: { groups: string[]; nodes: string[] };
  error: string | null;
  /** True where the hub database is missing, so nodes named only through a group are not counted. */
  groups_unknown?: boolean;
}

export interface ProjectWorkspaceStatus {
  /** Absent for an ops or design project, which has no workspace. */
  workspace?: string;
  kind?: 'ops' | 'design';
  present: boolean;
  head?: string | null;
  branch?: string | null;
  modified?: number;
}

export interface ProjectEntry {
  id: string;
  source: 'builtin' | 'custom';
  data: ProjectData;
  errors: string[];
  status?: ProjectWorkspaceStatus;
  /** ops projects only. */
  targets?: ProjectTargetsSummary;
}

export interface ProjectList {
  projects: ProjectEntry[];
  custom_dir: string;
}

export interface ProjectSyncResult {
  id: string;
  ok: boolean;
  action?: 'cloned' | 'updated' | 'skipped';
  reason?: string;
  error?: string;
  head?: string | null;
  branch?: string | null;
}

// --- Node groups (user-defined, many-to-many; docs/design/node-groups.md) ---------------

export type GroupField = 'tier' | 'arch' | 'guardian_role' | 'role' | 'tailscale_tags' | 'cli_support' | 'name';
export type GroupOp = 'eq' | 'ne' | 'contains' | 'not_contains' | 'glob';

export interface GroupCondition {
  field: GroupField;
  op: GroupOp;
  value: string;
}

/** All conditions must match (AND); a group matches a node when any of its rules does (OR). */
export interface GroupRule {
  conditions: GroupCondition[];
}

export interface NodeGroupData {
  /** Omitted on create: the hub derives a slug from the name. */
  id?: string;
  name: string;
  description?: string;
  tone?: 'ok' | 'warn' | 'crit' | 'info' | 'idle' | null;
  include: string[];
  rules: GroupRule[];
  exclude: string[];
}

export interface NodeGroup extends NodeGroupData {
  id: string;
  /** Node ids resolved against the hub's servers.json (include ∪ rule matches − exclude). */
  members: string[];
  warnings: string[];
  created_at?: string;
  updated_at?: string;
}

export interface GroupCatalogField {
  field: GroupField;
  kind: 'scalar' | 'list';
  ops: GroupOp[];
  /** Distinct values seen in servers.json, offered as suggestions. */
  values: string[];
}

export interface NodeGroupList {
  groups: NodeGroup[];
  ungrouped: string[];
  catalog: { fields: GroupCatalogField[]; tones: string[] };
  servers_file: string | null;
  warnings: string[];
}

export interface GroupSaveResult {
  id: string;
  members: string[];
  warnings: string[];
}

export interface GroupPreview {
  members: string[];
  warnings: string[];
}


// --- Operator decisions: key revoke, audit, closed threads, workspace reset, publications ---

/** GET /api/nodes/{id}/revoke-key: what revoking the hub key on a node would remove. */
export interface RevokeKeyPreview {
  node: string;
  target: string;
  fingerprint: string;
  match: string;
  note?: string;
}

/** POST /api/nodes/{id}/revoke-key. */
export interface RevokeKeyResult {
  ok: boolean;
  removed: number;
  note?: string;
}

/** One entry of GET /api/audit (newest first). */
export interface AuditEntry {
  ts: string;
  actor?: string;
  route: string;
  targets: string[];
  command: string;
  exit_codes?: Record<string, number>;
  duration_ms?: number;
  /** `ok`, `error`, or `rejected` (the hub refused the request). */
  status: 'ok' | 'error' | 'rejected' | string;
  /** Free-text note, e.g. why a request was rejected. */
  detail?: string;
}

/** GET /api/projects/{id}/workspace-reset: the plan. */
export interface WorkspaceResetPlan {
  ok: boolean;
  project_id: string;
  workspace: string;
  discard: Array<{ path: string; state: string }>;
  keep: Array<{ path: string; action_id: number | string; thread_id?: string }>;
}

/** POST /api/projects/{id}/workspace-reset. */
export interface WorkspaceResetResult {
  ok: boolean;
  discarded: string[];
}

/** One row of GET /api/threads/{thread}/publications (newest first). */
export interface Publication {
  id: number | string;
  branch: string;
  commit_sha?: string | null;
  remote?: string;
  status: string;
  error?: string | null;
  actor?: string;
  created_at?: string;
}

// --- Design artifacts (docs/design/design-project.md, P3): read-only views of the version store ---

/** One file of an artifact version; `expired` files keep their entry (a tombstone) but have no content. */
export interface ArtifactFile {
  path: string;
  sha256: string;
  bytes: number;
  mime: string;
  /** 'markdown' | 'mermaid' | 'svg' | 'docx' | 'pptx' (from the hub's extension table). */
  kind: string;
  stage: string;
  agent: string;
  title: string;
  expired?: boolean;
  /** Derived files only (docs/design/office-export.md): the Markdown file it was built from and that file's sha256. */
  source?: string;
  source_sha256?: string;
  converted_at?: string;
}

/** Response of `GET /api/threads/{thread}/artifacts/{version}`. */
export interface ArtifactManifest {
  manifest_version: number;
  version: number;
  project: string;
  thread: string;
  created_at: string;
  parent: number | null;
  written: string;
  expired_at?: string;
  files: ArtifactFile[];
}

/** Counts of one version, as the list endpoints report them (expired files are not counted). */
export interface ArtifactSummary {
  version: number;
  created_at: string;
  written: string;
  files: number;
  bytes: number;
  expired_at?: string;
}

/** Response of `GET /api/projects/{id}/artifacts`: threads with artifacts, newest first. */
export interface ArtifactProjectList {
  project: string;
  threads: Array<{ thread_id: string; versions: number; latest: ArtifactSummary }>;
}

/** One thread of `GET /api/projects/{id}/results`: a debate report and/or design artifacts, joined by thread id. */
export interface ResultItem {
  thread_id: string;
  /** Report title, or the thread id when the report has none. */
  title: string;
  /** Raw decision word (`APPROVED`, `DISPUTED`, ...); empty when the report head has none. */
  status: string;
  rounds: number;
  /** RFC 3339 UTC; empty when unknown. */
  updated_at: string;
  has_report: boolean;
  report_bytes: number;
  artifact_versions: number;
  artifact_files: number;
}

/** Response of `GET /api/projects/{id}/results`: newest first, capped by the hub (`total` counts all). */
export interface ProjectResults {
  project: string;
  items: ResultItem[];
  total: number;
}

/** Response of `GET /api/threads/{thread}/artifacts`. */
export interface ArtifactVersionList {
  thread_id: string;
  project: string;
  versions: ArtifactSummary[];
}

export interface PackageRoleInfo {
  name: string;
  description: string;
  /** Protected package name patterns per package manager. */
  protected: Record<string, string[]>;
}

export interface PackageRoleList {
  ok: boolean;
  error?: string;
  managers: string[];
  roles: PackageRoleInfo[];
}

/** Worker state as the hub's fresh ping saw it; `off` nodes are not pinged. */
export type WorkerState = 'up' | 'down' | 'off';

export interface WorkerRow {
  id: string;
  name: string;
  enabled: boolean;
  /** The capability probe passed on this node. */
  capable: boolean;
  /** ISO time of the last probe, if any. */
  checked?: string | null;
  max_jobs?: number | null;
  state: WorkerState;
  error?: string | null;
  version?: string;
  /** The worker runs the code the hub would install now. */
  current?: boolean;
  protocol?: number;
  free_slots?: number;
  providers?: string[];
  git?: boolean;
}

export interface WorkerReport {
  protocol: number;
  code_version: string;
  workers: WorkerRow[];
  /** The inventory could not be read. */
  error?: string;
}

// --- Solo agent (Quick ask, docs/design/solo-agent.md) -------------------------

/** Body of `POST /api/solo`: one role of a project answers one question alone (read-only, no Judge). */
export interface SoloStart {
  question: string;
  /** General question (no project): general knowledge only, so no project, context paths or ref may be sent. */
  general?: boolean;
  project?: string;
  /** Persona id; empty means the default team's expert (a role that only judges is refused). */
  role?: string;
  /** Provider id (`claude`, `codex`, ...); empty means the first available one, with fallback. */
  provider?: string;
  /** Answer language; empty means the CLI's default (Korean). */
  language?: string;
  context_paths?: string[];
  context_ref?: string;
}

/** Response of `POST /api/solo`: the hub answers 202 with the id while the CLI still runs. */
export interface SoloStartResult {
  id: string;
  status: string;
  label: string;
  note: string;
}

/** `result.json` of a solo run, served unchanged by `GET /api/solo/{id}`. */
export interface SoloResult {
  id: string;
  label: string;
  note: string;
  /** Answer mode: `general` answered from general knowledge alone (no project, no sources); absent on older hubs. */
  mode?: 'code' | 'ops' | 'general';
  project: string;
  /** Persona id of the role that answered. */
  role: string;
  /** Provider that answered (`claude-cli`, ...; `built-in-expert` when degraded). */
  provider: string;
  question: string;
  /** Markdown; may end with a machine-readable ```findings block the view hides. */
  answer: string;
  /** True when every provider failed and the built-in fallback answered. */
  degraded: boolean;
  citations: { verified: string[]; unshown: string[]; missing: string[] };
  summary: string;
  steps?: number | null;
  failures: string[];
  /** RFC 3339 UTC. */
  created: string;
  elapsed_s: number;
}

/** Response of `GET /api/solo/{id}`. */
export interface SoloView {
  id: string;
  status: 'running' | 'completed' | 'failed';
  label: string;
  note: string;
  /** Answer mode of the run (`general` has no project); absent on older hubs. */
  mode?: 'code' | 'ops' | 'general';
  error?: string;
  result?: SoloResult;
}

/** One row of `GET /api/solo` (recent solo runs, newest first). */
export interface SoloListItem {
  id: string;
  status: 'running' | 'completed' | 'failed';
  label: string;
  note: string;
  /** Answer mode (`general` answered from general knowledge); absent on older hubs and running/failed rows. */
  mode?: 'code' | 'ops' | 'general';
  error?: string;
  project?: string;
  role?: string;
  provider?: string;
  question?: string;
  summary?: string;
  degraded?: boolean;
  created?: string;
  elapsed_s?: number;
}

// --- Quick review (docs/design/verification-layer.md, V1) ---------------------

/**
 * Request of `POST /api/reviews`: exactly one change source — `range` (BASE..HEAD), `patch`, `ref` (V2a, branch of
 * the project's origin; `base` and `expect_sha` only with it) or `action` (V2b, a PROPOSED `jev_mcp:apply_patch`).
 */
export interface ReviewStart {
  project: string;
  range?: string;
  /** Unified diff; applied to `patch_base` (default HEAD) in a temporary index on the hub. */
  patch?: string;
  patch_base?: string;
  /** Branch of the project's configured origin; the hub fetches it into a private namespace. */
  ref?: string;
  /** Ref expression the branch is compared against; default the merge-base with the project branch. */
  base?: string;
  /** 40 lowercase hex; the review refuses to start when the fetched head differs. */
  expect_sha?: string;
  /** Decision DB id of the action whose edits are reviewed. */
  action?: number;
  /** `session:<workstation>`; stamped by the hub's link entry, never sent by this dashboard. */
  started_by?: string;
  /** Persona id; empty means the default team's first critic. */
  role?: string;
  provider?: string;
  language?: string;
}

export interface ReviewStartResult {
  id: string;
  status: string;
  label: string;
  note: string;
}

export type ReviewSeverity = 'high' | 'medium' | 'low';

/** One finding (or note) of a review: verified quote, claim and failure scenario. */
export interface ReviewFinding {
  path: string;
  line: number;
  quote: string;
  claim: string;
  scenario: string;
  severity: ReviewSeverity;
  verified: boolean;
  reason: string;
}

/**
 * Which change was reviewed (V2): a `ref` review names the branch, an `action` review the action id; `base`/`head`
 * are commit shas there. The V1 kinds keep their `change` fields above.
 */
export interface ReviewSourceInfo {
  kind: 'range' | 'patch' | 'ref' | 'action' | string;
  ref?: string;
  base?: string;
  head?: string;
  /** The `--base` ref as requested; empty when the merge-base was used. */
  base_ref?: string;
  action_id?: number;
}

/** `request.json` the hub stores at start (V2c): what was asked for, never the patch text. */
export interface ReviewRequest {
  project: string;
  source: {
    kind: 'range' | 'patch' | 'ref' | 'action' | string;
    range?: string;
    ref?: string;
    base?: string;
    expect_sha?: string;
    action_id?: number;
  };
  /** `session:<workstation>` when a session tool started it; absent when a person did. */
  started_by?: string;
}

/** `result.json` of a review, served unchanged by `GET /api/reviews/{id}`. */
export interface ReviewResult {
  id: string;
  label: string;
  note: string;
  /** done, bad_format (no readable findings block) or degraded (no provider answered). */
  state: 'done' | 'bad_format' | 'degraded';
  project: string;
  role: string;
  provider: string;
  change: { range?: string; patch?: boolean };
  /** Which change was reviewed (V2); absent on records written before it. */
  source?: ReviewSourceInfo;
  base: string;
  head: string;
  files: string[];
  added: number;
  removed: number;
  findings: ReviewFinding[];
  notes: ReviewFinding[];
  summary: string;
  /** The answer text when no findings block could be read (masked). */
  text?: string;
  degraded: boolean;
  failures: string[];
  created: string;
  elapsed_s: number;
}

export interface ReviewView {
  id: string;
  status: 'running' | 'completed' | 'failed';
  label: string;
  note: string;
  error?: string;
  result?: ReviewResult;
  /** The stored start request (V2c), omitted on records written before it. */
  request?: ReviewRequest;
}

export interface ReviewListItem {
  id: string;
  status: 'running' | 'completed' | 'failed';
  label: string;
  note: string;
  error?: string;
  state?: ReviewResult['state'];
  project?: string;
  range?: string;
  patch?: boolean;
  head?: string;
  role?: string;
  provider?: string;
  files?: number;
  summary?: string;
  counts?: { high: number; medium: number; low: number; notes: number };
  created?: string;
  elapsed_s?: number;
  /** From the stored request (V2c), so they show while the review runs: kind, branch and action id. */
  source_kind?: 'range' | 'patch' | 'ref' | 'action' | string;
  ref?: string;
  action_id?: number;
  /** `session:<workstation>` when a session tool started it. */
  started_by?: string;
}

/** Response of `GET /api/reviews`. */
export interface ReviewList {
  runs: ReviewListItem[];
}

/** Response of `GET /api/solo`. */
export interface SoloList {
  runs: SoloListItem[];
}

// --- Resident agent monitor (docs/design/resident-monitor.md) -------------------
// One read-only snapshot of the mail store (`GET /api/residents/monitor`). Every store field may
// be null (a store older or newer than the hub still renders); only identity fields are fixed.

/** The open incarnation of a resident agent, if any. */
export interface ResidentIncarnation {
  epoch: number | null;
  started: string | null;
  pane: string | null;
  pid: number | null;
}

/** The agent's open turn (a turn with no `ended`), if any. */
export interface ResidentOpenTurn {
  id: string | null;
  kind: string | null;
  started: string | null;
  hop_in: number | null;
}

/** Counts of open mail to one agent (`queued` / `delivered` / `retrieved` states). */
export interface ResidentMailOpen {
  queued: number | null;
  delivered: number | null;
  retrieved: number | null;
}

/** One resident agent: the store row joined with the declaration-file entry. */
export interface ResidentAgent {
  address: string;
  name: string | null;
  node: string | null;
  provider: string | null;
  driver: string | null;
  /** `closed` or `open`, from the declaration file. */
  tools: string | null;
  state: string | null;
  epoch: number | null;
  not_ready_since: string | null;
  bind_failures: number | null;
  bind_started: string | null;
  modified: string | null;
  incarnation: ResidentIncarnation | null;
  turn: ResidentOpenTurn | null;
  mail_open: ResidentMailOpen;
  tasks_held: number | null;
  session_ready?: boolean;
  queries_pending?: number;
  /** True when the declaration file names the agent. */
  declared: boolean;
  /** True when the store holds a row for it. */
  in_store: boolean;
}

/** One turn of one agent; `ended` is null while it runs. */
export interface ResidentTurn {
  id: string | null;
  epoch: number | null;
  kind: string | null;
  started: string | null;
  ended: string | null;
  end_reason: string | null;
  hop_in: number | null;
  inputs: number | null;
}

/** One mail row: metadata and size only; the body is read on demand. */
export interface ResidentMail {
  id: string;
  thread: string | null;
  sender: string | null;
  recipient: string | null;
  kind: string | null;
  subject: string | null;
  task: string | null;
  reply_to: string | null;
  hop: number | null;
  state: string | null;
  created: string | null;
  expires: string | null;
  redelivered: number | null;
  bytes: number | null;
}

/** One task of the board; patch and call log are reduced to sizes and counts. */
export interface ResidentTask {
  id: string;
  title: string | null;
  creator: string | null;
  assignee: string | null;
  owner: string | null;
  status: string | null;
  project: string | null;
  sha: string | null;
  hop: number | null;
  generation: number | null;
  blocked_by: Array<string | null>;
  patch_bytes: number | null;
  /** Decision-DB id of the action the patch became; joins the Approvals screen's action list. */
  patch_action: number | null;
  calls: number | null;
  reason: string | null;
  created: string | null;
  modified: string | null;
  expires: string | null;
}

/** One store event; `detail` holds writer-defined ids, counts, states and reasons (never a body). */
export interface ResidentEvent {
  id: number | null;
  at: string | null;
  kind: string | null;
  agent: string | null;
  detail: Record<string, unknown> | null;
}

/** One published resident artifact metadata record (PD4). */
export interface ResidentArtifact {
  id: string;
  agent: string;
  task: string | null;
  title: string;
  filename: string;
  rel_path: string;
  mime: string;
  sha256: string;
  size_bytes: number;
  summary: string;
  pinned: boolean;
  created: string;
  expires: string;
}

/** One direct resident session query metadata record (resident-session-query.md, QD1-QD5; resident-multi-session.md). */
export interface ResidentQuery {
  id: string;
  address: string;
  project: string;
  project_kind: string;
  session_key?: string;
  task: string | null;
  epoch: number;
  session_id: string;
  caller: string;
  caller_kind: string;
  mode: string;
  status: string;
  turn_id: string | null;
  answer_truncated: boolean;
  error: string | null;
  timeout_sec: number;
  created: string;
  started: string | null;
  completed: string | null;
  expires: string;
}

/** One direct resident session query with secret-masked prompt and answer (`GET /api/residents/queries/{id}`). */
export interface ResidentQueryDetail extends ResidentQuery {
  prompt: string | null;
  answer: string | null;
}

/** Response of `POST /api/residents/queries` (202 Accepted). */
export interface ResidentQuerySubmit {
  ok: boolean;
  id: string;
  address: string;
  project: string;
  project_kind: string;
  session_key?: string;
  epoch: number;
  session_id: string;
  mode?: string;
  status: string;
}

/** One scoped provider session row (`agent_sessions`, resident-multi-session.md). */
export interface ResidentScopedSession {
  address: string;
  epoch: number;
  session_key: string;
  project: string | null;
  label: string | null;
  provider_session_id: string | null;
  session_ready: boolean;
  status: string;
  turns: number;
  created: string;
  last_used: string | null;
  closed: string | null;
}

/** Response of `GET /api/residents/monitor`. */
export interface ResidentMonitor {
  generated: string | null;
  store: { exists: boolean; schema: number | null; path_kind: string | null };
  agents: ResidentAgent[];
  /** Recent turns per agent address. */
  turns: Record<string, ResidentTurn[]>;
  mail: ResidentMail[];
  tasks: ResidentTask[];
  events: ResidentEvent[];
  artifacts?: ResidentArtifact[];
  queries?: ResidentQuery[];
  sessions?: ResidentScopedSession[];
  problems: string[];
}

/** Response of `GET /api/residents/mail/{id}`: the body, secrets masked on the hub. */
export interface ResidentMailBody {
  id: string;
  body: string | null;
  bytes: number | null;
  masked: boolean | null;
}

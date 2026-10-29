import type {
  ActionItem,
  DebateJob,
  DebateJobDetail,
  DebateResult,
  PersonaConfig,
  PersonaList,
  TeamList,
} from '../src/types';

export const personaList: PersonaList = {
  personas: [
    { id: 'sec-expert', name: 'Security Expert', role: 'Domain Expert Agent', domain: 'Linux hardening', system_prompt: 'You harden hosts.', source: 'builtin' },
    { id: 'ops-critic', name: 'Ops Critic', role: 'Critic Agent', domain: 'Operations risk', system_prompt: 'You find risks.', source: 'builtin' },
    { id: 'perf-critic', name: 'Perf Critic', role: 'Critic Agent', domain: 'Performance', system_prompt: 'You find slow paths.', source: 'custom' },
    { id: 'jev-judge', name: 'JEV Judge', role: 'JEV-MCP Decision Agent', domain: 'Decisions', system_prompt: 'You decide.', source: 'builtin' },
  ],
  custom_dir: '/home/ubuntu/.guardian/data/personas',
};

export const teamList: TeamList = {
  custom_dir: '/home/ubuntu/.guardian/data/teams',
  teams: [
    {
      id: 'cross-review',
      name: 'Cross Review',
      source: 'builtin',
      data: {
        id: 'cross-review',
        name: 'Cross Review',
        expert: { persona: 'sec-expert', providers: ['claude', 'codex'] },
        critic: [{ persona: 'ops-critic', providers: ['agy'] }, 'perf-critic'],
        judge: { persona: 'jev-judge', providers: ['claude'] },
        max_rounds: 3,
        context: { paths: ['apps/agent/*.go', 'cli/**/*.py'], ref: 'main' },
        category: 'development',
        created: '2026-10-04T12:00:00+09:00',
      },
      errors: [],
      warnings: ['codex is not installed on the hub'],
    },
    {
      id: 'broken',
      name: 'Broken Team',
      source: 'custom',
      data: { id: 'broken', name: 'Broken Team', expert: 'ghost', critic: [], judge: 'jev-judge' },
      errors: ['unknown persona: ghost'],
      warnings: [],
    },
  ],
  catalog: {
    providers: ['claude', 'codex', 'agy', 'ollama'],
    installed: ['claude', 'agy'],
    tools: [
      { name: 'jev_mcp:run_tests', risk: 'low', description: 'Run the test suite' },
      { name: 'jev_mcp:apply_patch', risk: 'medium', description: 'Apply edits to files' },
      { name: 'server_remediation', risk: 'high', description: 'Run a remediation on a node' },
    ],
    max_critics: 3,
    max_rounds: 6,
    categories: ['development', 'operations', 'design-docs', 'general'],
  },
};

export const runningJob: DebateJob = {
  id: 'job1',
  task: 'Harden sshd',
  team: 'cross-review',
  thread_id: '',
  rounds: 3,
  status: 'running',
  started_at: '2026-10-04T10:00:00Z',
  finished_at: '',
  exit_code: null,
  error: '',
};

export const doneResult: DebateResult = {
  thread_id: 'thread_abc123',
  team: { id: 'cross-review', name: 'Cross Review' },
  consensus_reached: true,
  log_file: '/home/ubuntu/.guardian/logs/thread_abc123.md',
  context: {
    ref: 'main',
    sha: '0123456789abcdef0123456789abcdef01234567',
    paths: ['apps/agent/*.go'],
    files: [
      { path: 'apps/agent/main.go', bytes: 10240, truncated: true },
      { path: 'apps/agent/exec.go', bytes: 2048, truncated: false, parts: ['#executeSSH', 'lines 110-145'], outline: true },
    ],
    skipped: [{ path: 'secrets.enc', reason: 'secret pattern' }],
    total_bytes: 12288,
    tree_files: 87,
  },
  final_decision: {
    approved: false,
    unverified: false,
    jev_gate: 'DISPUTED',
    final_round: 2,
    judgment: 'Judge says adopt the hardened approach.',
    decision: 'Disable password login after verifying key access.',
    checks: ['sshd -t passes', 'PasswordAuthentication is no'],
    jev: { reasons: ['The rollback step is not verified'] },
    actions: [
      { id: 41, tool: 'jev_mcp:apply_patch', status: 'HELD' },
      { id: 42, tool: 'run_tests', status: 'FAILED', error: 'exit 1' },
    ],
  },
  transcript: [
    { round: 2, agent: 'Judge Bot', role: 'JEV-MCP Decision Agent', content: 'Final answer', provider: 'claude' },
    { round: 1, agent: 'Sec Expert', role: 'Domain Expert Agent', content: 'Proposal text <b>bold</b>', provider: 'agy', fallbacks: ['codex: not installed'] },
    { round: 1, agent: 'Ops Critic', role: 'Critic Agent', content: 'Critique text', provider: 'claude' },
  ],
};

export const doneJob: DebateJobDetail = {
  ...runningJob,
  status: 'completed',
  thread_id: 'thread_abc123',
  finished_at: '2026-10-04T10:05:00Z',
  exit_code: 0,
  context_paths: ['apps/agent/*.go'],
  log: 'round 1 done\nround 2 done',
  result: doneResult,
};

export function action(over: Partial<ActionItem> = {}): ActionItem {
  return {
    id: 57,
    thread_id: 'thread_abc123',
    round_num: 2,
    tool: 'run_tests',
    arguments: { command: 'go test ./...' },
    reason: 'Verify the change',
    status: 'PROPOSED',
    result: null,
    decided_by: null,
    created_at: '2026-10-04T10:06:00Z',
    updated_at: '2026-10-04T10:06:00Z',
    risk: 'low',
    tool_description: 'Runs tests on the hub workspace',
    base_sha: null,
    ...over,
  };
}

export const emptyPersona: PersonaConfig = personaList.personas[0];

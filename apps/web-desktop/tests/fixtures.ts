import type {
  AuthorizedKey,
  FleetDiagnosisReport,
  FleetSSHReport,
  SecurityRuleCheck,
  NodeGroupList,
  SecurityReport,
  FleetNode,
  SSHConfigSummary,
  SystemMetrics,
} from '../src/types';

export const metrics: SystemMetrics = {
  timestamp: '2026-10-04T10:00:00Z',
  hostname: 'hub-host',
  os: 'linux',
  arch: 'arm64',
  uptime_sec: 1000,
  cpu_usage_pct: 34.1,
  memory_total_bytes: 8 * 1024 ** 3,
  memory_used_bytes: 4 * 1024 ** 3,
  memory_pct: 50,
  disk_total_bytes: 100 * 1024 ** 3,
  disk_used_bytes: 91 * 1024 ** 3,
  disk_pct: 91,
  load_avg: [0.42, 0.4, 0.38],
  top_processes: [{ pid: 1204, user: 'guardian', cpu: 12.4, mem: 3.1, command: '/usr/local/bin/guardian-agent --sse' }],
};

export const servers: FleetNode[] = [
  { id: 'a1', name: 'site-a-arm1', hostname: '10.0.0.1', user: 'u', port: 22, arch: 'arm64', group: 'Site A', key: 'k', recommended: true, description: '', tailscale_ip: '100.64.0.1' },
  { id: 'b2', name: 'site-b-ec1', hostname: '10.0.0.2', user: 'u', port: 22, arch: 'amd64', group: 'Site B', key: 'k', recommended: false, description: '' },
];

export const diagnosis: FleetDiagnosisReport = {
  total_nodes: 2,
  online_count: 1,
  duration_sec: 3.456,
  results: [
    { id: 'a1', name: 'site-a-arm1', group: 'Site A', arch: 'arm64', status: 'ONLINE', uptime: 'up 3 days', load_avg: '0.1 0.2 0.3', memory: '4G/8G', disk: '48%', duration_ms: 120 },
    { id: 'b2', name: 'site-b-ec1', group: 'Site B', arch: 'amd64', status: 'OFFLINE', uptime: '', duration_ms: 2000 },
  ],
};

export const security: SecurityReport = {
  score: 72,
  assessed_at: '2026-10-04T10:00:00Z',
  pass_count: 5,
  failed_count: 1,
  warn_count: 0,
  info_count: 0,
  rules: [
    { rule_id: 'r1', title: 'Disable root login', severity: 'HIGH', status: 'FAIL', current_value: 'yes', expected_value: 'no', remediation: 'Set PermitRootLogin no' },
    { rule_id: 'r2', title: 'Use protocol 2', severity: 'LOW', status: 'PASS', current_value: '2', expected_value: '2', remediation: '' },
  ],
};

export const sshConfig: SSHConfigSummary = {
  config_path: '/etc/ssh/sshd_config',
  port: 2222,
  permit_root_login: 'yes',
  password_authentication: 'no',
  pubkey_authentication: 'yes',
  max_auth_tries: 4,
  x11_forwarding: 'no',
  allow_tcp_forwarding: 'yes',
  is_valid: true,
  syntax_checked: true,
  syntax_message: '',
  source: 'sshd -T',
  raw_lines_count: 40,
};

export const keys: AuthorizedKey[] = [
  { id: 'k1', key_type: 'ssh-ed25519', public_key: 'AAAA', comment: 'william@laptop', fingerprint: 'SHA256:abcdef' },
];

/** a1 is in two groups (it shows under both); b2 only in Site B; "Empty" has no members. */
export const groupList: NodeGroupList = {
  groups: [
    { id: 'site-a', name: 'Site A', include: ['a1'], rules: [], exclude: [], members: ['a1'], warnings: [] },
    {
      id: 'arm',
      name: 'ARM',
      description: 'every ARM node',
      include: [],
      rules: [{ conditions: [{ field: 'arch', op: 'eq', value: 'arm64' }] }],
      exclude: [],
      members: ['a1'],
      warnings: [],
    },
    { id: 'site-b', name: 'Site B', include: ['b2'], rules: [], exclude: [], members: ['b2'], warnings: [] },
    { id: 'empty', name: 'Empty', include: ['gone'], rules: [], exclude: [], members: [], warnings: ['include: gone is not in servers.json'] },
  ],
  ungrouped: [],
  catalog: {
    fields: [
      { field: 'arch', kind: 'scalar', ops: ['eq', 'ne'], values: ['amd64', 'arm64'] },
      { field: 'name', kind: 'scalar', ops: ['glob', 'eq', 'ne'], values: ['site-a-arm1', 'site-b-ec1'] },
    ],
    tones: ['ok', 'warn', 'crit', 'info', 'idle'],
  },
  servers_file: '/srv/configs/servers.json',
  warnings: [],
};

const rule = (rule_id: string, title: string, status: SecurityRuleCheck['status'], current = '', expected = '', remediation = ''): SecurityRuleCheck => ({
  rule_id,
  title,
  severity: status === 'PASS' ? 'PASS' : 'HIGH',
  status,
  current_value: current,
  expected_value: expected,
  remediation,
});

const RULES = [
  { rule_id: 'SEC-SSH-001', title: 'Disable Direct Root Login via SSH' },
  { rule_id: 'SEC-SSH-002', title: 'Enforce Key-Based Authentication Only' },
  { rule_id: 'SEC-SSH-005', title: 'Custom Non-standard SSH Port' },
];

/** a1 audited (ok, no FAIL); b2 partial with two FAIL rules; c3 unreachable and in no group. */
export const fleetSSH: FleetSSHReport = {
  generated_at: '2026-10-05T03:00:00Z',
  duration_ms: 2310,
  cached: false,
  nodes: [
    {
      id: 'a1',
      name: 'site-a-arm1',
      status: 'ok',
      source: 'sshd -T (sudo)',
      login_user: 'ubuntu',
      duration_ms: 640,
      config: { ...sshConfig, source: 'sshd -T' },
      report: {
        score: 85,
        assessed_at: '2026-10-05T03:00:00Z',
        rules: [
          rule('SEC-SSH-001', 'Disable Direct Root Login via SSH', 'WARN', 'prohibit-password', 'no', 'Prefer PermitRootLogin no'),
          rule('SEC-SSH-002', 'Enforce Key-Based Authentication Only', 'PASS', 'no', 'no'),
          rule('SEC-SSH-005', 'Custom Non-standard SSH Port', 'INFO', '22', 'Non-22'),
        ],
        failed_count: 0,
        warn_count: 1,
        info_count: 1,
        pass_count: 1,
      },
      keys: [{ id: 'key-1', key_type: 'ssh-ed25519', public_key: '', comment: 'guardian-hub-fleet@hub', fingerprint: 'SHA256:hubkey', options: 'from="100.64.0.7"' }],
      notes: [],
    },
    {
      id: 'b2',
      name: 'site-b-ec1',
      status: 'partial',
      source: 'sshd_config (fallback)',
      login_user: 'opc',
      duration_ms: 900,
      config: { ...sshConfig, source: 'sshd_config (fallback)', syntax_checked: false },
      report: {
        score: 40,
        assessed_at: '2026-10-05T03:00:00Z',
        rules: [
          rule('SEC-SSH-001', 'Disable Direct Root Login via SSH', 'FAIL', 'yes', 'no', "Set 'PermitRootLogin no'"),
          rule('SEC-SSH-002', 'Enforce Key-Based Authentication Only', 'FAIL', 'yes', 'no', "Set 'PasswordAuthentication no'"),
          rule('SEC-SSH-005', 'Custom Non-standard SSH Port', 'PASS', '2222', 'Non-22'),
        ],
        failed_count: 2,
        warn_count: 0,
        info_count: 0,
        pass_count: 1,
      },
      keys: [],
      notes: ['sshd -T could not run (needs root; sudo -n refused)'],
    },
    {
      id: 'c3',
      name: 'lonely',
      status: 'unreachable',
      duration_ms: 4000,
      keys: [],
      notes: [],
      error: 'ssh: connect to host lonely port 22: No route to host',
    },
  ],
  summary: {
    nodes: 3,
    audited: 2,
    unreachable: 1,
    errors: 0,
    avg_score: 62.5,
    failing_nodes: ['b2'],
    rules: RULES,
    by_rule: {},
  },
};

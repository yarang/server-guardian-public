import { describe, expect, it } from 'vitest';
import { buildDebateTask, buildNodeDebateTask } from '../src/lib/debateTask';
import { ago, duration } from '../src/lib/format';
import { resultLines } from '../src/lib/terminalLines';
import { clearDebatePrefill, consumeDebatePrefill, getDebatePrefill, setDebatePrefill } from '../src/state/debatePrefill';
import { diagnosis, fleetSSH, security, servers } from './fixtures';

describe('debate prefill store', () => {
  it('sets, reads, consumes and clears', () => {
    expect(getDebatePrefill()).toBeNull();
    setDebatePrefill('task text', 'assistant');
    expect(getDebatePrefill()?.task).toBe('task text');
    expect(consumeDebatePrefill()?.source).toBe('assistant');
    expect(getDebatePrefill()).toBeNull();
    clearDebatePrefill();
  });
});

describe('buildDebateTask', () => {
  it('uses only the supplied data', () => {
    const task = buildDebateTask({ diagnosis, security });
    expect(task).toContain('1/2 nodes online');
    expect(task).toContain('72/100');
  });

  it('states unavailable sources with their reasons', () => {
    const task = buildDebateTask({ diagnosis: null, diagnosisError: 'x', security: null, securityError: 'y' });
    expect(task).toContain('Fleet diagnosis: not available (x).');
    expect(task).toContain('SSH security audit: not available (y).');
  });
});

describe('buildDebateTask scope', () => {
  it('reachability keeps only the probe, even when other data is given', () => {
    const task = buildDebateTask({ scope: 'reachability', diagnosis, security, fleetSsh: fleetSSH });
    expect(task).toContain('Scope: fleet reachability only.');
    expect(task).toContain('Offline: site-b-ec1');
    expect(task).not.toContain('SSH');
    expect(task).not.toContain('72/100');
  });

  it('reachability states a missing probe', () => {
    expect(buildDebateTask({ scope: 'reachability', diagnosis: null, diagnosisError: 'boom' })).toContain('Fleet diagnosis: not available (boom).');
  });

  it('ssh covers the fleet audit: failing nodes with their FAIL rules and the nodes that were not audited', () => {
    const task = buildDebateTask({ scope: 'ssh', diagnosis, security, fleetSsh: fleetSSH });
    expect(task).toContain('Scope: the fleet SSH audit only.');
    expect(task).toContain('Fleet SSH audit (scope: 2 of 3 nodes audited): average score 62.5/100, 1 with failing rules.');
    expect(task).toContain('- site-b-ec1 (score 40):');
    expect(task).toContain('  - [HIGH] Disable Direct Root Login via SSH: current "yes", expected "no"');
    expect(task).toContain('Not audited: lonely (unreachable)');
    expect(task).not.toContain('site-a-arm1');
    expect(task).not.toContain('Fleet diagnosis');
    expect(task).not.toContain("hub's own sshd");
  });

  it('ssh states a missing audit with its reason', () => {
    expect(buildDebateTask({ scope: 'ssh', diagnosis: null, fleetSsh: null, fleetSshError: 'audit down' })).toContain('Fleet SSH audit: not available (audit down).');
  });

  it('all with a fleet audit adds it and leaves the hub-only score out unless supplied', () => {
    const task = buildDebateTask({ diagnosis, fleetSsh: fleetSSH });
    expect(task).toContain('Fleet diagnosis (scope: all 2 nodes)');
    expect(task).toContain('Fleet SSH audit (scope: 2 of 3 nodes audited)');
    expect(task).not.toContain("hub's own sshd");
    expect(task).not.toContain('SSH security audit');
  });
});

describe('buildNodeDebateTask', () => {
  it('uses the node, its probe and its audit only', () => {
    const task = buildNodeDebateTask(servers[0], diagnosis.results[0], fleetSSH.nodes[0]);
    expect(task).toContain('node site-a-arm1 (a1)');
    expect(task).toContain('Inventory: host 10.0.0.1, arch arm64, mesh IP 100.64.0.1.');
    expect(task).toContain('Probe: ONLINE, uptime up 3 days, load 0.1 0.2 0.3, memory 4G/8G, disk 48%.');
    expect(task).toContain('SSH audit: score 85/100 (0 failed, 1 passed).');
    expect(task).toContain('[HIGH] WARN Disable Direct Root Login via SSH: current "prohibit-password", expected "no"');
  });

  it('states missing probe and audit instead of inventing them', () => {
    const task = buildNodeDebateTask(servers[1], null, null);
    expect(task).toContain('Probe: not available.');
    expect(task).toContain('SSH audit: not available.');
    expect(buildNodeDebateTask(servers[1], diagnosis.results[1], fleetSSH.nodes[2])).toContain('SSH audit: not audited (unreachable: ssh: connect to host lonely port 22: No route to host).');
  });
});

describe('resultLines', () => {
  it('renders -1 results as errors with the exit code', () => {
    const lines = resultLines({ exit_code: -1, stdout: '', stderr: 'Command was not run: boom', duration_ms: 0 });
    expect(lines).toEqual([
      { kind: 'err', text: 'Command was not run: boom' },
      { kind: 'meta', text: 'exit -1 · 0 ms' },
    ]);
  });
});

describe('duration', () => {
  it('formats seconds compactly and treats 0 as unknown', () => {
    expect(duration(0)).toBe('—');
    expect(duration(42 * 60)).toBe('42분');
    expect(duration(5 * 3600 + 12 * 60)).toBe('5시간 12분');
    expect(duration(3 * 86400 + 4 * 3600 + 59)).toBe('3일 4시간');
    expect(duration('x')).toBe('—');
  });
});

describe('ago', () => {
  it('words the age of a measurement and never goes negative', () => {
    const now = 1_000_000_000;
    expect(ago(now - 2000, now)).toBe('방금');
    expect(ago(now + 5000, now)).toBe('방금');
    expect(ago(now - 42_000, now)).toBe('42초 전');
    expect(ago(now - 5 * 60_000, now)).toBe('5분 전');
    expect(ago(now - 3 * 3600_000, now)).toBe('3시간 전');
    expect(ago(now - 50 * 3600_000, now)).toBe('2일 전');
  });
});

describe('retrieval formatting', () => {
  it('formats bytes in B, KB and MB', async () => {
    const { fmtBytes, retrievalText } = await import('../src/lib/format');
    expect(fmtBytes(512)).toBe('512 B');
    expect(fmtBytes(2048)).toBe('2.0 KB');
    expect(fmtBytes(2 * 1024 * 1024)).toBe('2.0 MB');
    expect(fmtBytes(undefined)).toBe('0 B');
    expect(retrievalText({ results: 176, bytes: 2 * 1024 * 1024 })).toBe('읽음 176 · 2.0 MB');
    expect(retrievalText(undefined)).toBe('');
  });
});

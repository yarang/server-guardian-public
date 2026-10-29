import { isOnline } from './reachability';
import type { FleetDiagnosisReport, FleetNodeResult, FleetSSHNode, FleetSSHReport, NodeGroup, SecurityReport, FleetNode } from '../types';
import { fixed } from './format';
import { groupReachability, rulesText } from './groups';
import { findings } from './sshAudit';

/** What a debate task covers: everything on hand, only fleet reachability, or only the fleet SSH audit. */
export type DebateScope = 'all' | 'reachability' | 'ssh';

export interface DebateTaskInput {
  /** Defaults to 'all'. */
  scope?: DebateScope;
  diagnosis: FleetDiagnosisReport | null;
  diagnosisError?: string | null;
  /**
   * The hub-only sshd audit. Omit (undefined) to leave the section out; null states it as unavailable.
   * Not used by the 'reachability' and 'ssh' scopes.
   */
  security?: SecurityReport | null;
  securityError?: string | null;
  /** Fleet-wide SSH audit (every node); undefined leaves the section out, null states it as unavailable. */
  fleetSsh?: FleetSSHReport | null;
  fleetSshError?: string | null;
}

function reachabilityLines(input: DebateTaskInput): string[] {
  const d = input.diagnosis;
  if (!d) return [`Fleet diagnosis: not available${input.diagnosisError ? ` (${input.diagnosisError})` : ''}.`];
  const results = d.results || [];
  const online = results.length ? results.filter(isOnline).length : d.online_count;
  const lines = [`Fleet diagnosis (scope: all ${d.total_nodes} nodes): ${online}/${d.total_nodes} nodes online (probe took ${fixed(d.duration_sec, 2)}s).`];
  const offline = results.filter((r) => !isOnline(r)).map((r) => (r.reachability && r.reachability !== 'ok' ? `${r.name} (${r.reachability})` : r.name));
  if (offline.length) lines.push(`Offline: ${offline.join(', ')}`);
  return lines;
}

function hubSecurityLines(input: DebateTaskInput): string[] {
  const s = input.security;
  if (!s) return [`SSH security audit: not available${input.securityError ? ` (${input.securityError})` : ''}.`];
  const lines = [`SSH security score (scope: the hub's own sshd only): ${s.score}/100 (${s.failed_count} failed, ${s.pass_count} passed).`];
  const bad = (s.rules || []).filter((r) => r.status !== 'PASS');
  if (bad.length) {
    lines.push('Failing rules:');
    for (const r of bad) {
      lines.push(`- [${r.severity}] ${r.title}: current "${r.current_value}", expected "${r.expected_value}"`);
    }
  }
  return lines;
}

function fleetSshLines(input: DebateTaskInput): string[] {
  const f = input.fleetSsh;
  if (!f) return [`Fleet SSH audit: not available${input.fleetSshError ? ` (${input.fleetSshError})` : ''}.`];
  const s = f.summary;
  const avg = s.avg_score === null ? 'no node audited' : `average score ${s.avg_score}/100`;
  const lines = [`Fleet SSH audit (scope: ${s.audited} of ${s.nodes} nodes audited): ${avg}, ${s.failing_nodes.length} with failing rules.`];
  const failing = f.nodes.filter((n) => (n.report?.failed_count ?? 0) > 0);
  for (const n of failing) {
    lines.push(`- ${n.name} (score ${n.report?.score}):`);
    for (const r of findings(n).filter((x) => x.status === 'FAIL')) {
      lines.push(`  - [${r.severity}] ${r.title}: current "${r.current_value}", expected "${r.expected_value}"`);
    }
  }
  const unaudited = f.nodes.filter((n) => !n.report);
  if (unaudited.length) lines.push(`Not audited: ${unaudited.map((n) => `${n.name} (${n.status})`).join(', ')}`);
  return lines;
}

/**
 * Builds the Debate task text from real hub data only. Missing sources are stated as unavailable
 * (with the reason) rather than filled in. `scope` limits the task to one card's facts.
 */
export function buildDebateTask(input: DebateTaskInput): string {
  const scope = input.scope ?? 'all';
  if (scope === 'reachability') {
    return ['Review the reachability of the Server Guardian fleet and propose remediation actions. Scope: fleet reachability only.', '', ...reachabilityLines(input)].join('\n');
  }
  if (scope === 'ssh') {
    return ['Review the SSH security of the Server Guardian fleet and propose remediation actions. Scope: the fleet SSH audit only.', '', ...fleetSshLines(input)].join('\n');
  }

  const lines: string[] = ['Review the current state of the Server Guardian fleet and propose remediation actions.', ''];
  lines.push(...reachabilityLines(input), '');
  if (input.fleetSsh !== undefined || input.fleetSshError) lines.push(...fleetSshLines(input), '');
  if (input.security !== undefined || input.securityError || input.fleetSsh === undefined) lines.push(...hubSecurityLines(input));
  return lines.join('\n').trimEnd();
}

/** A task about one node: its inventory entry, probe result and SSH audit, whichever are available. */
export function buildNodeDebateTask(node: FleetNode, probe: FleetNodeResult | null, audit: FleetSSHNode | null): string {
  const lines = [`Review the Server Guardian node ${node.name} (${node.id}) and propose remediation actions. Scope: this node only.`, ''];
  lines.push(`Inventory: host ${node.hostname}, arch ${node.arch}${node.tailscale_ip ? `, mesh IP ${node.tailscale_ip}` : ''}.`);
  if (probe) {
    lines.push(`Probe: ${probe.status}${probe.reachability ? ` (${probe.reachability})` : ''}${isOnline(probe) ? `, uptime ${probe.uptime || 'unknown'}` : ''}${probe.load_avg ? `, load ${probe.load_avg}` : ''}${probe.memory ? `, memory ${probe.memory}` : ''}${probe.disk ? `, disk ${probe.disk}` : ''}.`);
  } else {
    lines.push('Probe: not available.');
  }
  if (!audit) {
    lines.push('SSH audit: not available.');
  } else if (!audit.report) {
    lines.push(`SSH audit: not audited (${audit.status}${audit.error ? `: ${audit.error}` : ''}).`);
  } else {
    lines.push(`SSH audit: score ${audit.report.score}/100 (${audit.report.failed_count} failed, ${audit.report.pass_count} passed).`);
    const bad = findings(audit);
    if (bad.length) {
      lines.push('Findings:');
      for (const r of bad) lines.push(`- [${r.severity}] ${r.status} ${r.title}: current "${r.current_value}", expected "${r.expected_value}"`);
    }
  }
  return lines.join('\n');
}

/**
 * A task about one node group: its definition, member reachability and the members' SSH audit,
 * whichever are available. `ssh` undefined leaves the audit section out; null states it as unavailable.
 */
export function buildGroupDebateTask(group: NodeGroup, servers: FleetNode[], diagnosis: FleetDiagnosisReport | null, ssh?: FleetSSHReport | null): string {
  const names = new Map(servers.map((s) => [s.id, s.name]));
  const nameOf = (id: string) => names.get(id) ?? id;
  const lines = [`Review the Server Guardian node group "${group.name}" (${group.id}) and propose remediation actions. Scope: the ${group.members.length} members of this group only.`, ''];
  const def: string[] = [];
  if (group.include.length) def.push(`${group.include.length} picked`);
  if (group.rules.length) def.push(`rules: ${rulesText(group.rules)}`);
  if (group.exclude.length) def.push(`${group.exclude.length} excluded`);
  lines.push(`Group: ${def.join('; ') || 'no definition'}.`);
  lines.push(`Members: ${group.members.length ? group.members.map(nameOf).join(', ') : 'none'}.`);

  const r = groupReachability(group, servers, diagnosis);
  if (!r.probed) {
    lines.push('Reachability: not available (no fleet diagnosis).');
  } else {
    lines.push(`Reachability: ${r.online}/${r.total} members online${r.notProbed ? `, ${r.notProbed} not probed` : ''}.`);
    if (r.offline.length) lines.push(`Offline: ${r.offline.join(', ')}`);
  }

  if (ssh === null) {
    lines.push('SSH audit: not available.');
  } else if (ssh) {
    const own = ssh.nodes.filter((n) => group.members.includes(n.id));
    const scored = own.filter((n) => n.report);
    lines.push(`SSH audit (scope: ${scored.length} of ${own.length} members audited).`);
    for (const n of scored.filter((x) => (x.report?.failed_count ?? 0) > 0)) {
      lines.push(`- ${n.name} (score ${n.report?.score}):`);
      for (const f of findings(n).filter((x) => x.status === 'FAIL')) {
        lines.push(`  - [${f.severity}] ${f.title}: current "${f.current_value}", expected "${f.expected_value}"`);
      }
    }
    const unaudited = own.filter((n) => !n.report);
    if (unaudited.length) lines.push(`Not audited: ${unaudited.map((n) => `${n.name} (${n.status})`).join(', ')}`);
  }
  return lines.join('\n');
}

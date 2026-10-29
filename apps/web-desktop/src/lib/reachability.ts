import type { Tone } from '@server-guardian/ui';
import { tr } from '../i18n';
import type { Reachability } from '../types';

/** Anything the hub classifies: fleet diagnosis rows, exec results, SSH audit nodes. `status` is the pre-reachability field. */
export interface ReachabilityCarrier {
  reachability?: Reachability;
  status?: string;
}

export interface ReachabilityInfo {
  tone: Tone;
  label: string;
}

const KNOWN: Record<Reachability, ReachabilityInfo> = {
  ok: { tone: 'ok', label: tr.shell.reachability.ok },
  command_failed: { tone: 'warn', label: tr.shell.reachability.commandFailed },
  auth_failed: { tone: 'crit', label: tr.shell.reachability.authFailed },
  host_key_changed: { tone: 'crit', label: tr.shell.reachability.hostKeyChanged },
  unreachable: { tone: 'crit', label: tr.shell.reachability.unreachable },
};

function known(r: ReachabilityCarrier): Reachability | null {
  return r.reachability && r.reachability in KNOWN ? r.reachability : null;
}

/** Chip tone and text; falls back to the old ONLINE/OFFLINE status when an older hub sends no reachability. */
export function reachabilityInfo(r: ReachabilityCarrier): ReachabilityInfo {
  const k = known(r);
  if (k) return KNOWN[k];
  return r.status === 'ONLINE' ? KNOWN.ok : { tone: 'crit', label: tr.shell.reachability.offline };
}

/** Online means ssh worked: reachability ok or command_failed (old hubs: status ONLINE). */
export function isOnline(r: ReachabilityCarrier): boolean {
  const k = known(r);
  if (k) return k === 'ok' || k === 'command_failed';
  return r.status === 'ONLINE';
}

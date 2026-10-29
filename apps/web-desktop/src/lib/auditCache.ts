import type { FleetSSHReport } from '../types';

/** How long a fleet SSH audit stays "fresh" for the Summary, which never starts an audit on its own. */
export const AUDIT_FRESH_MS = 5 * 60 * 1000;

let last: { report: FleetSSHReport; at: number } | null = null;

/** Remembers the latest fleet audit (from the SSH audit tab or the Summary's Run audit button). */
export function rememberAudit(report: FleetSSHReport, now: number = Date.now()): void {
  last = { report, at: now };
}

/** The remembered audit when it is younger than {@link AUDIT_FRESH_MS}, else null. */
export function freshAudit(now: number = Date.now()): FleetSSHReport | null {
  return last && now - last.at < AUDIT_FRESH_MS ? last.report : null;
}

export function clearAuditCache(): void {
  last = null;
}

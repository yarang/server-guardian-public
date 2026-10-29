import { tr } from '../i18n';
import type { AuditEntry } from '../types';

/** Operator-facing name of an audit route; unknown routes are shown as sent. */
export function routeLabel(route: string): string {
  if (/\/revoke-key\/?$/.test(route)) return tr.shell.audit.route.revokeKey;
  if (route.startsWith('/api/nodes')) return tr.shell.audit.route.node;
  switch (route.toLowerCase()) {
    case 'local':
    case 'hub':
      return tr.shell.audit.route.local;
    case 'node':
    case 'ssh':
    case 'server':
      return tr.shell.audit.route.node;
    case 'group':
      return tr.shell.audit.route.group;
    case 'fleet':
    case 'all':
      return tr.shell.audit.route.fleet;
    default:
      return route || '—';
  }
}

/** "exit 0", "N건 모두 exit 0" or "5건 중 2건 실패" for the per-target exit codes of a run. */
export function exitSummary(entry: Pick<AuditEntry, 'exit_codes'>): string {
  const codes = Object.values(entry.exit_codes ?? {});
  if (codes.length === 0) return '—';
  const failed = codes.filter((c) => c !== 0).length;
  if (failed === 0) return codes.length === 1 ? 'exit 0' : tr.shell.audit.allExitOk(codes.length);
  if (codes.length === 1) return `exit ${codes[0]}`;
  return tr.shell.audit.failedOf(failed, codes.length);
}

/** Chip tone and label of an audit status; a rejected request is a refusal, not a command failure. */
export function auditStatus(status: string): { tone: 'ok' | 'warn' | 'crit'; label: string } {
  if (status === 'ok') return { tone: 'ok', label: tr.shell.audit.status.ok };
  if (status === 'rejected') return { tone: 'warn', label: tr.shell.audit.status.rejected };
  return { tone: 'crit', label: tr.shell.audit.status.error };
}

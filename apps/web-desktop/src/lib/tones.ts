import type { Tone } from '@server-guardian/ui';
import { tr } from '../i18n';
import type { SecurityRuleCheck } from '../types';

export function scoreTone(score: number): Tone {
  if (score >= 80) return 'ok';
  if (score >= 50) return 'warn';
  return 'crit';
}

export function ruleTone(status: SecurityRuleCheck['status']): Tone {
  if (status === 'PASS') return 'ok';
  if (status === 'WARN') return 'warn';
  if (status === 'INFO') return 'info';
  return 'crit';
}

export function severityTone(severity: SecurityRuleCheck['severity']): Tone {
  if (severity === 'CRITICAL' || severity === 'HIGH') return 'crit';
  if (severity === 'MEDIUM') return 'warn';
  if (severity === 'LOW') return 'info';
  return 'idle';
}

/** "실패 2 · 경고 1 · 정보 1 · 통과 1": only FAIL rules count as failed. */
export function auditCounts(s: { failed_count: number; warn_count?: number; info_count?: number; pass_count: number }): string {
  const parts = [tr.shell.tones.failed(s.failed_count)];
  if (s.warn_count) parts.push(tr.shell.tones.warnings(s.warn_count));
  if (s.info_count) parts.push(tr.shell.tones.info(s.info_count));
  parts.push(tr.shell.tones.passed(s.pass_count));
  return parts.join(' · ');
}

/** Usage thresholds shared by the meters: warn from 70 %, critical from 85 %. */
export const USAGE_THRESHOLDS = { warn: 70, crit: 85 };

export function usageTone(value: number): Tone {
  if (value >= USAGE_THRESHOLDS.crit) return 'crit';
  if (value >= USAGE_THRESHOLDS.warn) return 'warn';
  return 'ok';
}

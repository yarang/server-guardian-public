import { locale, tr } from '../i18n';

/** Fixed-decimals number formatting that never renders NaN/undefined as a value. */
export function fixed(value: unknown, digits = 1): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}

/** Clamp a percentage into 0-100 for Meter; non-numbers become 0. */
export function pct(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
}

/** Bytes to a GB string with the given decimals. */
export function gb(bytes: unknown, digits = 1): string {
  return typeof bytes === 'number' && Number.isFinite(bytes) ? (bytes / 1024 ** 3).toFixed(digits) : '—';
}

/** Message text of an unknown thrown value. */
export function errorText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

/** Locale of every date / time the dashboard formats: the active language's (i18n/index.ts). */
export const LOCALE = locale;

/** Local date and time ("2026. 10. 13. 오후 3:04:05") for a Date, epoch ms or ISO string; the input text when unparseable. */
export function fmtDateTime(value: Date | number | string): string {
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString(LOCALE);
}

/** "3일 4시간", "5시간 12분" or "42분" for a duration in seconds; "—" when unknown (0). */
export function duration(seconds: unknown): string {
  const s = typeof seconds === 'number' && Number.isFinite(seconds) ? Math.floor(seconds) : 0;
  if (s <= 0) return '—';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return tr.common.durationDH(d, h);
  if (h > 0) return tr.common.durationHM(h, m);
  return tr.common.durationM(m);
}

/** "12초 전", "5분 전", "3시간 전" or "2일 전" for a past epoch-ms time; "방금" under 5 s. */
export function ago(then: number, now: number = Date.now()): string {
  const s = Math.max(0, Math.floor((now - then) / 1000));
  if (s < 5) return tr.common.justNow;
  if (s < 60) return tr.common.secondsAgo(s);
  const m = Math.floor(s / 60);
  if (m < 60) return tr.common.minutesAgo(m);
  const h = Math.floor(m / 60);
  if (h < 24) return tr.common.hoursAgo(h);
  return tr.common.daysAgo(Math.floor(h / 24));
}

/** "512 B", "48.3 KB", "2.0 MB" or "1.5 GB" for a byte count; non-numbers become "0 B". */
export function fmtBytes(bytes: unknown): string {
  const n = typeof bytes === 'number' && Number.isFinite(bytes) ? Math.max(0, bytes) : 0;
  if (n < 1024) return `${Math.round(n)} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

/** Compact retrieval cost, e.g. "읽음 176 · 2.0 MB"; empty when the run reported none. */
export function retrievalText(r: { results: number; bytes: number } | null | undefined): string {
  return r ? tr.common.retrieval(r.results, fmtBytes(r.bytes)) : '';
}

/**
 * Short duration ("8일 15시간", "3시간 12분", "45분", "3일") from `uptime` output such as
 * "up 8 days, 15:45", "up 3:12", "up 45 min" or "10:00:01 up 6 days, 12:42, 2 users, load average: ...".
 * Returns "—" when the text holds no parseable "up ..." duration.
 */
export function shortUptime(text: unknown): string {
  if (typeof text !== 'string') return '—';
  const m = /\bup\s+(.*?)(?:,\s*\d+\s+users?\b|,?\s*load averages?:|$)/i.exec(text);
  if (!m) return '—';
  let days = 0;
  let hours = 0;
  let mins = 0;
  let found = false;
  for (const part of m[1].split(',')) {
    const p = part.trim();
    const clock = /^(\d+):(\d{1,2})$/.exec(p);
    const unitMatch = /^(\d+)\s*(week|day|hr|hour|min|minute)s?$/i.exec(p);
    if (clock) {
      hours += Number(clock[1]);
      mins += Number(clock[2]);
      found = true;
    } else if (unitMatch) {
      const n = Number(unitMatch[1]);
      const unit = unitMatch[2].toLowerCase();
      if (unit === 'week') days += n * 7;
      else if (unit === 'day') days += n;
      else if (unit === 'hr' || unit === 'hour') hours += n;
      else mins += n;
      found = true;
    }
  }
  if (!found) return '—';
  hours += Math.floor(mins / 60);
  mins %= 60;
  days += Math.floor(hours / 24);
  hours %= 24;
  if (days > 0) return hours > 0 ? tr.common.durationDH(days, hours) : tr.common.durationD(days);
  if (hours > 0) return tr.common.durationHM(hours, mins);
  return tr.common.durationM(mins);
}

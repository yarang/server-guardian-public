import type { TerminalLine } from '@server-guardian/ui';
import type { CommandResult } from '../api';

function splitOutput(text: string): string[] {
  return text.replace(/\n+$/, '').split('\n');
}

/**
 * Turn a command result into terminal lines. A failed request arrives from the api layer as
 * exit code -1 with the reason in stderr, and is rendered as an error, never as success.
 */
export function resultLines(res: CommandResult, label?: string): TerminalLine[] {
  const tag = label ? `[${label}] ` : '';
  const lines: TerminalLine[] = [];
  if (res.stdout) for (const t of splitOutput(res.stdout)) lines.push({ kind: 'out', text: `${tag}${t}` });
  if (res.stderr) for (const t of splitOutput(res.stderr)) lines.push({ kind: 'err', text: `${tag}${t}` });
  lines.push({ kind: 'meta', text: `${tag}exit ${res.exit_code} · ${res.duration_ms} ms` });
  return lines;
}

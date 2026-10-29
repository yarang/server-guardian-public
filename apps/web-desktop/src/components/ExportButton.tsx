import { useState } from 'react';
import type { ReactNode } from 'react';
import { Button } from '@server-guardian/ui';
import { AuthRequiredError } from '../api';
import { tr } from '../i18n';
import { exportChooseRoot, exportGetRoot, exportReveal, inDesktopShell } from '../lib/exportFolder';
import { errorText } from '../lib/format';
import { Notice } from './Notice';

/** What a run reports back: the counts, plus the threads that failed when a run covers several. */
export interface ExportOutcomeSummary {
  written: number;
  skipped: number;
  renamed: number;
  failed?: Array<{ thread: string; message: string }>;
}

export type ExportRun = (progress: (done: number, total: number) => void) => Promise<ExportOutcomeSummary>;

/**
 * "Save to folder" in the desktop shell (`button` is null in a browser). Asks for the export folder first when none
 * is set, runs `run`, then `notice` says how many files were written, skipped and renamed and offers "Show folder".
 * The button and the notice are separate so a toolbar can keep the button in its row and put the notice below it.
 * `run` reports progress as (done, total) for runs that cover several threads.
 */
export function useExportAction(run: ExportRun): {
  button: (label: string, disabled?: boolean) => ReactNode;
  notice: ReactNode;
} {
  const t = tr.shell.exportFolder;
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string; failed: string[]; reveal: boolean } | null>(null);

  const start = async () => {
    setResult(null);
    setBusy(t.working);
    try {
      const root = (await exportGetRoot()) ?? (await exportChooseRoot());
      if (!root) return;
      const s = await run((done, total) => setBusy(total > 1 ? t.progress(done, total) : t.working));
      const failed = (s.failed ?? []).map((f) => `${f.thread}: ${f.message}`);
      setResult({ ok: failed.length === 0, text: t.result(s.written, s.skipped, s.renamed), failed, reveal: true });
    } catch (e) {
      if (!(e instanceof AuthRequiredError)) setResult({ ok: false, text: t.saveFailed(errorText(e)), failed: [], reveal: false });
    } finally {
      setBusy(null);
    }
  };

  if (!inDesktopShell()) return { button: () => null, notice: null };
  return {
    button: (label, disabled) => (
      <Button size="sm" variant="outline" loading={busy !== null} disabled={disabled} onClick={() => void start()}>
        {busy ?? label}
      </Button>
    ),
    notice: result ? (
      <Notice tone={result.ok ? 'ok' : 'warn'}>
        {result.text}
        {result.failed.length > 0 ? `\n${t.failedThreads(result.failed.length)}\n${result.failed.join('\n')}` : ''}
        {result.reveal ? (
          <div style={{ marginTop: 'var(--sg-s-3)' }}>
            <Button size="sm" variant="ghost" onClick={() => void exportReveal().catch(() => undefined)}>
              {t.show}
            </Button>
          </div>
        ) : null}
      </Notice>
    ) : null,
  };
}

import { useEffect, useRef, useState } from 'react';
import { Button } from '@server-guardian/ui';
import { tr } from '../i18n';
import { AuthRequiredError } from '../api';
import { AUTO_EXPORT_EVENT, exportReveal, inDesktopShell } from '../lib/exportFolder';
import { runAutoExport } from '../lib/autoExport';
import type { AutoRun } from '../lib/autoExport';
import { useProjectStore } from '../state/projectStore';
import { Notice } from './Notice';
import { Row } from './layout';

/** How often the open app looks for finished results. The shell does the writing; this only asks it to. */
export const AUTO_EXPORT_INTERVAL_MS = 30_000;

/**
 * Runs automatic export while the desktop app is open and shows what it wrote (a silent write would be a surprise).
 * Renders nothing outside the shell and while the switch is off.
 */
export function AutoExport() {
  const { projects } = useProjectStore();
  const ids = (projects.data?.projects ?? []).map((p) => p.id).join('\n');
  const [notice, setNotice] = useState<AutoRun | null>(null);
  const failed = useRef(new Set<string>());
  const busy = useRef(false);

  useEffect(() => {
    if (!inDesktopShell() || ids === '') return;
    const list = ids.split('\n');
    let alive = true;

    const tick = async () => {
      if (busy.current) return;
      busy.current = true;
      try {
        const run = await runAutoExport(list, failed.current);
        if (!alive || !run) return;
        if (run.changedThreads > 0) {
          setNotice(run);
          window.dispatchEvent(new Event(AUTO_EXPORT_EVENT));
        }
      } catch (e) {
        // Sign-in lost, hub unreachable, shell hiccup: the next tick tries again.
        if (!(e instanceof AuthRequiredError)) return;
      } finally {
        busy.current = false;
      }
    };

    void tick();
    const timer = window.setInterval(tick, AUTO_EXPORT_INTERVAL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [ids]);

  if (!notice) return null;
  const t = tr.shell.exportFolder;
  const c = notice.counts;
  return (
    <div style={{ position: 'fixed', right: 'var(--sg-s-5)', bottom: 'var(--sg-s-5)', maxWidth: 420, zIndex: 50 }}>
      <Notice tone="ok">
        <div style={{ fontWeight: 600 }}>{t.auto.noticeTitle}</div>
        <div>{`${t.auto.noticeThreads(notice.changedThreads)}: ${t.result(c.written, c.skipped, c.renamed, c.updated)}`}</div>
        <Row>
          <Button size="sm" variant="outline" onClick={() => void exportReveal().catch(() => undefined)}>
            {t.show}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setNotice(null)}>
            {t.auto.dismiss}
          </Button>
        </Row>
      </Notice>
    </div>
  );
}

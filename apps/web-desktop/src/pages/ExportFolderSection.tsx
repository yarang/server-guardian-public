import { useCallback, useEffect, useState } from 'react';
import { Button, Panel } from '@server-guardian/ui';
import { tr } from '../i18n';
import { Notice } from '../components/Notice';
import { CheckboxField } from '../components/CheckboxField';
import { Row, Stack, mono, muted } from '../components/layout';
import {
  AUTO_EXPORT_EVENT,
  exportAutoGet,
  exportAutoRecent,
  exportAutoSet,
  exportChooseRoot,
  exportClearRoot,
  exportGetRoot,
  exportReveal,
} from '../lib/exportFolder';
import type { AutoRecent } from '../lib/exportFolder';

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** The automatic export switch and the last automatic writes; mounted per root, so a new root reloads both. */
function AutoExportSettings() {
  const t = tr.shell.exportFolder;
  const [enabled, setEnabled] = useState<boolean | undefined>(undefined);
  const [recent, setRecent] = useState<AutoRecent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const loadRecent = useCallback(() => exportAutoRecent().then(setRecent, () => undefined), []);

  useEffect(() => {
    let alive = true;
    exportAutoGet().then(
      (s) => alive && setEnabled(s.enabled),
      (e) => {
        if (!alive) return;
        setEnabled(false);
        setError(errorText(e));
      }
    );
    void loadRecent();
    window.addEventListener(AUTO_EXPORT_EVENT, loadRecent);
    return () => {
      alive = false;
      window.removeEventListener(AUTO_EXPORT_EVENT, loadRecent);
    };
  }, [loadRecent]);

  const toggle = async (next: boolean) => {
    setError(null);
    try {
      setEnabled((await exportAutoSet(next)).enabled);
    } catch (e) {
      setError(`${t.auto.failed} ${errorText(e)}`);
    }
  };

  return (
    <Stack gap="var(--sg-s-3)">
      <CheckboxField checked={enabled === true} disabled={enabled === undefined} onChange={(e) => void toggle(e.target.checked)}>
        {t.auto.label}
      </CheckboxField>
      <div style={{ ...muted, fontSize: 13 }}>{t.auto.hint}</div>
      {error ? <Notice tone="warn">{error}</Notice> : null}
      <div style={{ fontWeight: 600, fontSize: 13 }}>{t.auto.recentTitle}</div>
      {recent.length === 0 ? (
        <div style={{ ...muted, fontSize: 13 }}>{t.auto.recentNone}</div>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-5)', fontSize: 13 }}>
          {recent.map((r) => (
            <li key={`${r.atMs}/${r.project}/${r.thread}`}>
              <span style={mono}>{`${r.project}/${r.thread}`}</span>
              <span style={muted}>{` · ${new Date(r.atMs).toLocaleString()} · ${t.result(r.written, r.skipped, r.renamed, r.updated)}`}</span>
            </li>
          ))}
        </ul>
      )}
    </Stack>
  );
}

/** Settings row of the desktop shell: where "save to folder" writes (docs/design/desktop-export.md). */
export function ExportFolderSection() {
  const t = tr.shell.exportFolder;
  const [root, setRoot] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    exportGetRoot().then(
      (r) => alive && setRoot(r),
      (e) => {
        if (!alive) return;
        setRoot(null);
        setError(errorText(e));
      }
    );
    return () => {
      alive = false;
    };
  }, []);

  const run = useCallback(async (action: () => Promise<void>) => {
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  const choose = () =>
    run(async () => {
      const picked = await exportChooseRoot();
      if (picked) setRoot(picked);
    });
  const forget = () =>
    run(async () => {
      await exportClearRoot();
      setRoot(null);
    });

  return (
    <Panel title={t.title}>
      <Stack gap="var(--sg-s-3)">
        <Row>
          {root ? <span style={{ ...mono, overflowWrap: 'anywhere' }}>{root}</span> : <span style={muted}>{root === undefined ? '…' : t.none}</span>}
          <Button size="sm" variant="outline" onClick={choose}>
            {root ? t.change : t.choose}
          </Button>
          {root ? (
            <>
              <Button size="sm" variant="outline" onClick={() => run(exportReveal)}>
                {t.show}
              </Button>
              <Button size="sm" variant="outline" onClick={forget}>
                {t.forget}
              </Button>
            </>
          ) : null}
        </Row>
        <div style={{ ...muted, fontSize: 13 }}>{t.hint}</div>
        {root ? <AutoExportSettings key={root} /> : null}
        {error ? <Notice tone="warn">{`${t.failed} ${error}`}</Notice> : null}
      </Stack>
    </Panel>
  );
}

import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '@server-guardian/ui';
import { AuthRequiredError, HubError, fetchThreadReport, fetchThreadReportAsset, keepReport } from '../../api';
import { MarkdownDoc } from '../../components/MarkdownDoc';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { Row, muted } from '../../components/layout';
import { tr } from '../../i18n';
import { hrefFor } from '../../router';
import { useProjectKind } from '../../state/projectStore';
import { errorText } from '../../lib/format';
import { buildStandaloneMarkdown } from '../../lib/report';
import type { ThreadReport } from '../../types';

type Load = { phase: 'loading' } | { phase: 'missing' } | { phase: 'error'; message: string } | { phase: 'ready'; report: ThreadReport };

const loadAsset = (thread: string) => (name: string) => fetchThreadReportAsset(thread, name).then((a) => a.text);

/**
 * Saves text as `<fileName>`. Inside the Tauri window the WebView ignores `<a download>` for blob URLs,
 * so the desktop shell writes it to the Downloads folder (command `save_download`); in a plain browser
 * (dev server) a Blob link is used. Returns where it went.
 */
export async function saveTextFile(fileName: string, text: string, type = 'text/markdown'): Promise<string> {
  if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
    return invoke<string>('save_download', { fileName, contents: text });
  }
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const a = document.createElement('a');
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return fileName;
}

/**
 * A thread's Markdown result, fetched on mount: download button, load states and the rendered document.
 * `actions` sits at the right end of the toolbar (the dialog puts its close button there). Used by the report
 * dialog of a finished debate and inline by the Results tab. With `project` set, a report that belongs to another
 * project is not shown (the report route finds a thread by id across all projects).
 * "Keep as document" stores the report as an artifact version (docs/design/office-export.md); `keepable: false` leaves it
 * out when the caller knows better; a design project (whose documents are its artifacts, the hub refuses them) is
 * recognised from the project list, so the report dialog hides the button too.
 */
export function ReportView({
  thread,
  project,
  actions,
  keepable = true,
}: {
  thread: string;
  project?: string;
  actions?: ReactNode;
  keepable?: boolean;
}) {
  const [state, setState] = useState<Load>({ phase: 'loading' });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<{ ok: boolean; text: string } | null>(null);
  const [keeping, setKeeping] = useState(false);
  const [kept, setKept] = useState<{ tone: 'ok' | 'info' | 'crit'; text: string; link: boolean; details?: string[] } | null>(null);

  const load = useCallback(() => {
    setState({ phase: 'loading' });
    fetchThreadReport(thread).then(
      (report) => setState({ phase: 'ready', report }),
      (e: unknown) => {
        if (e instanceof AuthRequiredError) return;
        if (e instanceof HubError && e.status === 404) setState({ phase: 'missing' });
        else setState({ phase: 'error', message: errorText(e) });
      }
    );
  }, [thread]);
  useEffect(load, [load]);

  const reportProject = state.phase === 'ready' ? state.report.project : project;
  const design = useProjectKind(reportProject) === 'design';
  const foreign = state.phase === 'ready' && project !== undefined && state.report.project !== project;

  const download = async (report: ThreadReport) => {
    setSaving(true);
    setSaved(null);
    try {
      const md = await buildStandaloneMarkdown(report.markdown, thread, loadAsset(thread));
      const where = await saveTextFile(`${thread}.md`, md);
      setSaved({ ok: true, text: tr.debate.report.downloaded(where) });
    } catch (e) {
      setSaved({ ok: false, text: tr.debate.report.downloadFailed(errorText(e)) });
    } finally {
      setSaving(false);
    }
  };

  const keep = async () => {
    setKeeping(true);
    setKept(null);
    try {
      const r = await keepReport(thread);
      const text = r.warnings.length > 0 ? tr.debate.report.keptWarnings(r.warnings.length) : tr.debate.report.kept(r.version);
      setKept({ tone: 'ok', text, link: true, details: r.warnings });
    } catch (e) {
      if (e instanceof AuthRequiredError) return;
      if (e instanceof HubError && e.status === 409) setKept({ tone: 'info', text: tr.debate.report.alreadyKept, link: true });
      else setKept({ tone: 'crit', text: tr.debate.report.keepFailed(errorText(e)), link: false });
    } finally {
      setKeeping(false);
    }
  };

  const saveDiagram = async (code: string, position: number) => {
    setSaved(null);
    try {
      const where = await saveTextFile(`${thread}-diagram-${position}.mmd`, `${code}\n`, 'text/vnd.mermaid');
      setSaved({ ok: true, text: tr.debate.report.downloaded(where) });
    } catch (e) {
      setSaved({ ok: false, text: tr.debate.report.downloadFailed(errorText(e)) });
    }
  };

  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-5)', minWidth: 0 }}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Row>
          <Button
            size="sm"
            variant="outline"
            loading={saving}
            disabled={state.phase !== 'ready' || foreign}
            onClick={() => state.phase === 'ready' && void download(state.report)}
          >
            {tr.debate.report.download}
          </Button>
          {keepable && !design ? (
            <Button
              size="sm"
              variant="outline"
              loading={keeping}
              title={tr.debate.report.keepTitle}
              disabled={state.phase !== 'ready' || foreign}
              onClick={() => void keep()}
            >
              {keeping ? tr.debate.report.keeping : tr.debate.report.keep}
            </Button>
          ) : null}
        </Row>
        {actions}
      </Row>
      {kept ? (
        <Notice tone={kept.tone}>
          {kept.text}
          {kept.details && kept.details.length > 0 ? (
            <ul style={{ margin: 'var(--sg-s-3) 0 0', paddingLeft: 'var(--sg-s-6)', ...muted, fontSize: 12 }}>
              {kept.details.map((d) => (
                <li key={d} style={{ overflowWrap: 'anywhere' }}>
                  {d}
                </li>
              ))}
            </ul>
          ) : null}
          {kept.link ? (
            <div style={{ marginTop: 'var(--sg-s-3)' }}>
              <a href={hrefFor('debates', ['artifacts', thread])}>{tr.debate.report.openKept}</a>
            </div>
          ) : null}
        </Notice>
      ) : null}
      {saved ? <Notice tone={saved.ok ? 'ok' : 'crit'}>{saved.text}</Notice> : null}
      {state.phase === 'loading' ? <div style={{ ...muted, fontSize: 13 }}>{tr.debate.report.loading}</div> : null}
      {state.phase === 'missing' ? <Notice tone="info">{tr.debate.report.notFound}</Notice> : null}
      {state.phase === 'error' ? (
        <Notice tone="crit">
          {tr.debate.report.loadFailed(state.message)}
          <div style={{ marginTop: 'var(--sg-s-3)' }}>
            <Button size="sm" variant="outline" onClick={load}>
              {tr.debate.report.retry}
            </Button>
          </div>
        </Notice>
      ) : null}
      {foreign ? <Notice tone="info">{tr.debate.report.otherProject}</Notice> : null}
      {state.phase === 'ready' && !foreign ? <MarkdownDoc markdown={state.report.markdown} thread={thread} loadAsset={loadAsset(thread)} onSaveDiagram={(code, position) => void saveDiagram(code, position)} /> : null}
    </div>
  );
}

function ReportDialog({ thread, onClose }: { thread: string; onClose: () => void }) {
  return (
    <Modal title={tr.debate.report.title(thread)} onClose={onClose} width={960}>
      <ReportView
        thread={thread}
        actions={
          <Button size="sm" variant="ghost" onClick={onClose}>
            {tr.debate.report.close}
          </Button>
        }
      />
    </Modal>
  );
}

/** "결과 문서" button of a finished debate: opens the thread's Markdown result in a dialog (fetched on open). */
export function ReportButton({ thread }: { thread: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        {tr.debate.report.open}
      </Button>
      {open ? <ReportDialog thread={thread} onClose={() => setOpen(false)} /> : null}
    </>
  );
}

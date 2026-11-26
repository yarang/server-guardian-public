import { useEffect, useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { invoke } from '@tauri-apps/api/core';
import {
  AuthRequiredError,
  convertArtifact,
  fetchArtifactBlob,
  fetchArtifactManifest,
  fetchArtifactText,
  fetchArtifactVersions,
  fetchProjectArtifacts,
} from '../../api';
import { tr } from '../../i18n';
import { Ago } from '../../components/Ago';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { MarkdownDoc } from '../../components/MarkdownDoc';
import { Notice } from '../../components/Notice';
import { ProjectSwitcher } from '../../components/ProjectSwitcher';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { inDesktopShell } from '../../lib/shell';
import { OFFICE_FORMATS, artifactParams, artifactView, canMakeDerived, derivedFormat, mermaidAsMarkdown, parseArtifactRoute, svgDataUrl } from '../../lib/artifacts';
import type { ArtifactRoute, OfficeFormat } from '../../lib/artifacts';
import { parseTime } from '../../lib/debate';
import { errorText, fmtBytes } from '../../lib/format';
import { hrefFor } from '../../router';
import { useProjectStore } from '../../state/projectStore';
import type { ArtifactFile, ArtifactManifest, ArtifactSummary } from '../../types';

const href = (r: ArtifactRoute) => hrefFor('debates', artifactParams(r));

const linkCard = (selected: boolean) =>
  ({
    display: 'grid',
    gap: 'var(--sg-s-2)',
    padding: 'var(--sg-s-4) var(--sg-s-5)',
    background: selected ? 'var(--sg-surface-selected)' : 'var(--sg-surface-raised)',
    color: 'var(--sg-text)',
    border: `1px solid ${selected ? 'var(--sg-info-border)' : 'var(--sg-border)'}`,
    borderRadius: 'var(--sg-r-md)',
    fontSize: 13,
    textDecoration: 'none',
    minWidth: 0,
  }) as const;

function When({ iso }: { iso: string }) {
  const at = parseTime(iso);
  return at === null ? null : (
    <span style={{ ...muted, fontSize: 12 }}>
      <Ago at={at} />
    </span>
  );
}

/**
 * Saves a downloaded artifact. The text came through the authenticated API (cookie or bearer), so a Blob link works
 * in both modes without a token in the URL. The desktop shell can only write Markdown to Downloads (`save_download`).
 */
async function saveArtifact(file: ArtifactFile, text: string): Promise<string> {
  const base = file.path.split('/').pop() ?? file.path;
  if (inDesktopShell()) {
    if (file.kind !== 'markdown') throw new Error(tr.artifacts.desktopMarkdownOnly);
    return invoke<string>('save_download', { fileName: base, contents: text });
  }
  const url = URL.createObjectURL(new Blob([text], { type: `${file.mime};charset=utf-8` }));
  const a = document.createElement('a');
  a.href = url;
  a.download = base;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return base;
}

/**
 * Saves a derived Office file (binary, so it cannot take the text path above). Browser only: the desktop shell's
 * `save_download` writes text, and a Blob link does not download inside its webview.
 */
function saveDerived(file: ArtifactFile, blob: Blob): string {
  if (inDesktopShell()) throw new Error(tr.artifacts.desktopOfficeBrowserOnly);
  const base = file.path.split('/').pop() ?? file.path;
  const url = URL.createObjectURL(new Blob([blob], { type: file.mime }));
  const a = document.createElement('a');
  a.href = url;
  a.download = base;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return base;
}

function FileViewer({ project, thread, version, file }: { project: string; thread: string; version: number; file: ArtifactFile }) {
  const [state, setState] = useState<{ phase: 'loading' } | { phase: 'error'; message: string; status?: number } | { phase: 'ready'; text: string }>({
    phase: 'loading',
  });
  const [saved, setSaved] = useState<{ ok: boolean; text: string } | null>(null);
  const view = artifactView(file);

  useEffect(() => {
    if (file.expired || !view) return;
    const controller = new AbortController();
    setState({ phase: 'loading' });
    fetchArtifactText(thread, version, file.path, controller.signal).then(
      (text) => !controller.signal.aborted && setState({ phase: 'ready', text }),
      (e: unknown) => {
        if (controller.signal.aborted || e instanceof AuthRequiredError) return;
        setState({ phase: 'error', message: errorText(e), status: (e as { status?: number }).status });
      },
    );
    return () => controller.abort();
  }, [thread, version, file.path, file.expired, view]);

  const download = async () => {
    setSaved(null);
    try {
      if (derivedFormat(file)) {
        const saved = saveDerived(file, await fetchArtifactBlob(thread, version, file.path));
        setSaved({ ok: true, text: tr.artifacts.downloaded(saved) });
        return;
      }
      const text = state.phase === 'ready' ? state.text : await fetchArtifactText(thread, version, file.path);
      setSaved({ ok: true, text: tr.artifacts.downloaded(await saveArtifact(file, text)) });
    } catch (e) {
      if (!(e instanceof AuthRequiredError)) setSaved({ ok: false, text: tr.artifacts.downloadFailed(errorText(e)) });
    }
  };

  if (file.expired) return <Notice tone="info">{tr.artifacts.expiredFile}</Notice>;

  let body;
  const derived = derivedFormat(file);
  if (derived) {
    body = (
      <Stack gap="var(--sg-s-3)">
        {file.source ? <Notice tone="info">{tr.artifacts.office[derived].derivedFrom(file.source)}</Notice> : null}
        <span style={{ ...muted, fontSize: 12 }}>{tr.artifacts.officeIsCopy}</span>
      </Stack>
    );
  } else if (!view) body = <Notice tone="info">{tr.artifacts.unsupported}</Notice>;
  else if (state.phase === 'loading') body = <span style={muted}>{tr.artifacts.loadingFile}</span>;
  else if (state.phase === 'error') {
    body = (
      <Notice tone="crit">
        {state.status === 409 ? tr.artifacts.changed : tr.artifacts.fileFailed(file.path, state.message)}
      </Notice>
    );
  } else if (view === 'svg') {
    // Only ever an image: an <img> runs no script and loads nothing else.
    body = (
      <img
        src={svgDataUrl(state.text)}
        alt={tr.artifacts.svgAlt(file.path)}
        style={{ maxWidth: '100%', height: 'auto', background: 'var(--sg-surface-raised)', borderRadius: 'var(--sg-r-md)' }}
      />
    );
  } else {
    body = <MarkdownDoc markdown={view === 'mermaid' ? mermaidAsMarkdown(state.text) : state.text} topHeading={2} />;
  }

  return (
    <Card>
      <Row style={{ justifyContent: 'space-between' }}>
        <strong style={{ ...mono, fontSize: 13, overflowWrap: 'anywhere' }}>{file.path}</strong>
        <Button size="sm" variant="outline" onClick={() => void download()}>
          {tr.artifacts.download}
        </Button>
      </Row>
      {saved ? <Notice tone={saved.ok ? 'ok' : 'crit'}>{saved.text}</Notice> : null}
      {body}
    </Card>
  );
}

function FileRow({ file, route }: { file: ArtifactFile; route: ArtifactRoute }) {
  const selected = route.file === file.path;
  return (
    <a
      href={href({ ...route, file: file.path })}
      aria-current={selected ? 'true' : undefined}
      aria-label={tr.artifacts.openFile(file.path)}
      style={linkCard(selected)}
    >
      <div style={{ overflowWrap: 'anywhere' }}>{file.title || file.path}</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-3)' }}>
        <StatusChip tone={file.expired ? 'idle' : 'info'} title={file.kind}>
          {file.expired ? tr.artifacts.expiredFile : (tr.artifacts.kindLabel[file.kind] ?? file.kind)}
        </StatusChip>
        <span style={{ ...mono, ...muted, fontSize: 12 }}>{file.path}</span>
        <span style={{ ...muted, fontSize: 12 }}>{fmtBytes(file.bytes)}</span>
        {file.stage ? <span style={{ ...muted, fontSize: 12 }}>{tr.artifacts.stage(file.stage)}</span> : null}
        {file.agent ? <span style={{ ...muted, fontSize: 12 }}>{tr.artifacts.agent(file.agent)}</span> : null}
        <span style={{ ...mono, ...muted, fontSize: 11 }} title={file.sha256}>
          {tr.artifacts.sha(file.sha256.slice(0, 12))}
        </span>
      </div>
    </a>
  );
}

/** Asks the hub for a DOCX or PPTX of one Markdown file; the file joins the same version, so the listing is read again. */
function MakeOffice({ thread, version, file, format, onDone }: { thread: string; version: number; file: ArtifactFile; format: OfficeFormat; onDone: () => void }) {
  const [state, setState] = useState<{ phase: 'idle' } | { phase: 'busy' } | { phase: 'done'; ok: boolean; text: string }>({ phase: 'idle' });
  const make = async () => {
    setState({ phase: 'busy' });
    try {
      const made = await convertArtifact(thread, version, file.path, format);
      setState({ phase: 'done', ok: true, text: tr.artifacts.officeMade(made.file?.path ?? file.path) });
      onDone();
    } catch (e) {
      if (e instanceof AuthRequiredError) setState({ phase: 'idle' });
      else setState({ phase: 'done', ok: false, text: tr.artifacts.office[format].failed(errorText(e)) });
    }
  };
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-2)', justifyItems: 'start' }}>
      <Button
        size="sm"
        variant="outline"
        disabled={state.phase === 'busy'}
        title={tr.artifacts.office[format].makeTitle(file.path)}
        onClick={() => void make()}
      >
        {state.phase === 'busy' ? tr.artifacts.makingOffice : tr.artifacts.office[format].make}
      </Button>
      {state.phase === 'done' ? <Notice tone={state.ok ? 'ok' : 'crit'}>{state.text}</Notice> : null}
    </div>
  );
}

function VersionChips({ thread, versions, current }: { thread: string; versions: ArtifactSummary[]; current: number }) {
  return (
    <nav aria-label={tr.artifacts.versionsAria} style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-3)' }}>
      {[...versions].reverse().map((v, i) => (
        <a
          key={v.version}
          href={href({ thread, version: v.version })}
          aria-current={v.version === current ? 'true' : undefined}
          aria-label={tr.artifacts.openVersion(v.version)}
          style={{ ...linkCard(v.version === current), padding: 'var(--sg-s-3) var(--sg-s-5)', opacity: v.expired_at ? 0.6 : 1 }}
        >
          <span>
            {tr.artifacts.versionLabel(v.version)}
            {i === 0 ? ` · ${tr.artifacts.latest}` : ''}
          </span>
          <span style={{ ...muted, fontSize: 12 }}>{tr.artifacts.versionSummary(v.files, fmtBytes(v.bytes))}</span>
        </a>
      ))}
    </nav>
  );
}

function VersionFiles({ thread, version, route }: { thread: string; version: number; route: ArtifactRoute }) {
  const state = useHubData<ArtifactManifest>(() => fetchArtifactManifest(thread, version));
  return (
    <DataGate state={state} what={tr.artifacts.filesWhat}>
      {(m) => {
        const open = route.file ? m.files.find((f) => f.path === route.file) : undefined;
        return (
          <Stack gap="var(--sg-s-5)">
            {m.expired_at ? <Notice tone="info">{tr.artifacts.expiredVersion}</Notice> : null}
            {open ? (
              <>
                <div>
                  <a href={href({ thread, version })}>{tr.artifacts.backToFiles}</a>
                </div>
                <FileViewer key={`${version}/${open.path}`} project={m.project} thread={thread} version={version} file={open} />
              </>
            ) : m.files.length === 0 ? (
              <Notice tone="info">{tr.artifacts.noFiles}</Notice>
            ) : (
              <div style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
                {m.files.map((f) => (
                  <div key={f.path} style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
                    <FileRow file={f} route={{ thread, version }} />
                    {!m.expired_at
                      ? OFFICE_FORMATS.filter((format) => canMakeDerived(f, m.files, format)).map((format) => (
                          <MakeOffice key={format} thread={thread} version={version} file={f} format={format} onDone={state.reload} />
                        ))
                      : null}
                  </div>
                ))}
              </div>
            )}
          </Stack>
        );
      }}
    </DataGate>
  );
}

function ThreadView({ thread, route }: { thread: string; route: ArtifactRoute }) {
  const state = useHubData(() => fetchArtifactVersions(thread));
  return (
    <DataGate state={state} what={tr.artifacts.filesWhat}>
      {(list) => {
        const latest = list.versions[list.versions.length - 1]?.version ?? 1;
        const version = route.version ?? latest;
        return (
          <Stack gap="var(--sg-s-5)">
            <VersionChips thread={thread} versions={list.versions} current={version} />
            <VersionFiles key={`${thread}/${version}`} thread={thread} version={version} route={{ ...route, version }} />
          </Stack>
        );
      }}
    </DataGate>
  );
}

function ThreadList({ project }: { project: string }) {
  const state = useHubData(() => fetchProjectArtifacts(project));
  return (
    <DataGate state={state} what={tr.artifacts.threadsWhat}>
      {(list) =>
        list.threads.length === 0 ? (
          <Notice tone="info">{tr.artifacts.empty}</Notice>
        ) : (
          <nav aria-label={tr.artifacts.threadsAria} style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
            <strong style={{ fontSize: 13 }}>{tr.artifacts.threadsTitle(list.threads.length)}</strong>
            {list.threads.map((t) => (
              <a key={t.thread_id} href={href({ thread: t.thread_id })} aria-label={tr.artifacts.openThread(t.thread_id)} style={linkCard(false)}>
                <span style={{ ...mono, overflowWrap: 'anywhere' }}>{t.thread_id}</span>
                <span style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-3)', alignItems: 'center' }}>
                  <StatusChip tone="info">{tr.artifacts.versionCount(t.versions)}</StatusChip>
                  <span style={{ ...muted, fontSize: 12 }}>{tr.artifacts.versionSummary(t.latest.files, fmtBytes(t.latest.bytes))}</span>
                  <When iso={t.latest.created_at} />
                </span>
              </a>
            ))}
          </nav>
        )
      }
    </DataGate>
  );
}

/**
 * Artifacts of a design project: `#/debates/artifacts` lists the threads that have some, `/<thread>` shows the latest
 * version's files, `/<thread>/v<n>` another version, `/<thread>/v<n>/<file>` one file. Read-only; the hub serves
 * the files with a sandboxing CSP and verifies their sha256 on every read.
 */
export function ArtifactsTab({ params }: { params: string[] }) {
  const route = parseArtifactRoute(params);
  const { selected } = useProjectStore();
  return (
    <Stack>
      <ProjectSwitcher />
      {route.thread ? (
        <>
          <div>
            <a href={href({})}>{tr.artifacts.backToThreads}</a>
          </div>
          <ThreadView key={`${selected}/${route.thread}`} thread={route.thread} route={route} />
        </>
      ) : (
        <ThreadList key={selected} project={selected} />
      )}
    </Stack>
  );
}

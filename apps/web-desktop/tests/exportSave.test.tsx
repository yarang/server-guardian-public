import { act, fireEvent, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { ArtifactManifest, ArtifactVersionList, ProjectEntry, ProjectResults, ResultItem, ThreadReport } from '../src/types';

const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { ReportView } from '../src/pages/debate/ReportButton';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);
const t = ko.shell.exportFolder;

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

function Routed() {
  const { params } = useRoute();
  return <DebatesPage jobId={params[0]} wikiPage={params[1]} extra={params.slice(2)} />;
}

async function mount(hash: string) {
  window.location.hash = hash;
  render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle(300);
}

const click = async (name: string) => {
  fireEvent.click(screen.getByRole('button', { name }));
  await settle(50);
};

const project = (id: string, kind?: 'design' | 'ops'): ProjectEntry =>
  ({ id, source: 'custom', data: { id, name: id, kind }, errors: [], status: { present: true } }) as ProjectEntry;

const item = (thread_id: string, extra: Partial<ResultItem> = {}): ResultItem => ({
  thread_id, title: `Title of ${thread_id}`, status: 'APPROVED', rounds: 2, updated_at: '2026-10-21T09:00:00Z',
  has_report: true, report_bytes: 900, artifact_versions: 0, artifact_files: 0, ...extra,
});
const results = (project: string, items: ResultItem[]): ProjectResults => ({ project, items, total: items.length });
const report = (thread: string, project: string): ThreadReport => ({
  thread_id: thread, project, markdown: `# Report of ${thread}\n`, bytes: 20, updated_at: '2026-10-21T09:00:00Z', assets: [],
});

const sha = 'a'.repeat(64);
const file = (path: string, kind: string, extra: object = {}) => ({
  path, sha256: sha, bytes: 120, mime: kind === 'svg' ? 'image/svg+xml' : 'text/markdown', kind, stage: 'draft', agent: 'expert', title: path, ...extra,
});
const versions: ArtifactVersionList = {
  thread_id: 'thread_ab1', project: 'arch-docs',
  versions: [
    { version: 1, created_at: '2026-10-17T09:00:00Z', written: 'a', files: 1, bytes: 120, expired_at: '2026-10-18T00:00:00Z' },
    { version: 2, created_at: '2026-10-18T09:00:00Z', written: 'a', files: 3, bytes: 360 },
  ],
};
const manifest: ArtifactManifest = {
  manifest_version: 1, version: 2, project: 'arch-docs', thread: 'thread_ab1', created_at: '2026-10-18T09:00:00Z', parent: 1, written: 'a',
  files: [file('design.md', 'markdown'), file('flow.mmd', 'mermaid'), file('logo.svg', 'svg'), file('old.md', 'markdown', { expired: true }), file('data.bin', 'binary')],
};

/** The Rust side: a stored root (or none), a folder dialog answer, and `export_save` reporting every file as written. */
function fakeRust(root: string | null, picks: Array<string | null> = []) {
  invoke.mockImplementation(async (cmd: string, args?: { files?: Array<{ relPath: string }> }) => {
    if (cmd === 'export_get_root') return root;
    if (cmd === 'export_choose_root') {
      const next = picks.shift() ?? null;
      if (next) root = next;
      return next;
    }
    if (cmd === 'export_save') return (args?.files ?? []).map((f) => ({ relPath: f.relPath, outcome: 'written', savedAs: f.relPath }));
    if (cmd === 'export_reveal') return undefined;
    throw new Error(`unexpected command ${cmd}`);
  });
}
const saves = () => invoke.mock.calls.filter(([cmd]) => cmd === 'export_save').map(([, args]) => args);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-22T10:00:00Z'));
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  window.localStorage.setItem('guardian.project', 'server-guardian');
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.fetchProjects.mockResolvedValue({
    projects: [project('server-guardian'), project('arch-docs', 'design')],
    custom_dir: '/x',
  });
  mocked.fetchProjectResults.mockResolvedValue(results('server-guardian', [item('thread_a1'), item('thread_b2', { has_report: false }), item('thread_c3')]));
  mocked.fetchThreadReport.mockImplementation(async (thread) => report(thread, 'server-guardian'));
  mocked.fetchArtifactVersions.mockResolvedValue(versions);
  mocked.fetchArtifactManifest.mockResolvedValue(manifest);
  mocked.fetchArtifactText.mockImplementation(async (_t, _v, name) => `text of ${name}`);
});
afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  window.localStorage.clear();
  invoke.mockReset();
  vi.useRealTimers();
});

describe('Save to folder', () => {
  it('saves the report of every listed thread that has one, one call per thread', async () => {
    fakeRust('/Users/me/Guardian');
    await mount('#/debates/results');
    await click(t.saveAll);
    expect(saves()).toEqual([
      { project: 'server-guardian', thread: 'thread_a1', files: [{ relPath: 'report.md', contents: '# Report of thread_a1\n' }] },
      { project: 'server-guardian', thread: 'thread_c3', files: [{ relPath: 'report.md', contents: '# Report of thread_c3\n' }] },
    ]);
    expect(screen.getByText(t.result(2, 0, 0))).toBeTruthy();
  });

  it('is absent outside the desktop shell', async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await mount('#/debates/results');
    expect(screen.queryByRole('button', { name: t.saveAll })).toBeNull();
  });

  it('asks for a folder first, and writes nothing when the dialog is cancelled', async () => {
    fakeRust(null, [null]);
    await mount('#/debates/results');
    await click(t.saveAll);
    expect(invoke).toHaveBeenCalledWith('export_choose_root');
    expect(saves()).toEqual([]);
  });

  it('writes after the folder was just chosen', async () => {
    fakeRust(null, ['/Users/me/Guardian']);
    await mount('#/debates/results');
    await click(t.saveAll);
    expect(saves()).toHaveLength(2);
  });

  it('lists a thread that cannot be saved and carries on with the others', async () => {
    fakeRust('/Users/me/Guardian');
    mocked.fetchThreadReport.mockImplementation(async (thread) => report(thread, thread === 'thread_a1' ? 'other-project' : 'server-guardian'));
    await mount('#/debates/results');
    await click(t.saveAll);
    expect(saves().map((s) => (s as { thread: string }).thread)).toEqual(['thread_c3']);
    expect(screen.getByText(new RegExp('thread_a1: thread thread_a1 belongs to another project'))).toBeTruthy();
    expect(screen.getByText(new RegExp(t.failedThreads(1)))).toBeTruthy();
  });

  it('saves a design result as report.md plus the live versions, skipping expired and unsupported files', async () => {
    window.localStorage.setItem('guardian.project', 'arch-docs');
    fakeRust('/Users/me/Guardian');
    mocked.fetchProjectResults.mockResolvedValue(results('arch-docs', [item('thread_ab1', { artifact_versions: 2, artifact_files: 4 })]));
    mocked.fetchThreadReport.mockImplementation(async (thread) => report(thread, 'arch-docs'));
    await mount('#/debates/results/thread_ab1');
    await click(t.saveResult);
    expect(saves()).toEqual([
      {
        project: 'arch-docs',
        thread: 'thread_ab1',
        files: [
          { relPath: 'report.md', contents: '# Report of thread_ab1\n' },
          { relPath: 'v2/design.md', contents: 'text of design.md' },
          { relPath: 'v2/flow.mmd', contents: 'text of flow.mmd' },
          { relPath: 'v2/logo.svg', contents: 'text of logo.svg' },
        ],
      },
    ]);
    // The expired version 1 is never fetched.
    expect(mocked.fetchArtifactManifest).not.toHaveBeenCalledWith('thread_ab1', 1);
  });

  it('saves the report from the report view with the project the hub reports', async () => {
    fakeRust('/Users/me/Guardian');
    render(
      <GuardianRoot>
        <ReportView thread="thread_a1" project="server-guardian" />
      </GuardianRoot>
    );
    await settle(300);
    await click(t.saveReport);
    expect(saves()).toEqual([{ project: 'server-guardian', thread: 'thread_a1', files: [{ relPath: 'report.md', contents: '# Report of thread_a1\n' }] }]);
  });
});

describe('Artifacts and the export folder', () => {
  beforeEach(() => {
    window.localStorage.setItem('guardian.project', 'arch-docs');
    mocked.fetchProjectArtifacts.mockResolvedValue({ project: 'arch-docs', threads: [] });
  });

  it('saves one version from the file list', async () => {
    fakeRust('/Users/me/Guardian');
    await mount('#/debates/artifacts/thread_ab1/v2');
    await click(t.saveVersion);
    const [call] = saves() as Array<{ project: string; files: Array<{ relPath: string }> }>;
    expect(call.project).toBe('arch-docs');
    expect(call.files.map((f) => f.relPath)).toEqual(['v2/design.md', 'v2/flow.mmd', 'v2/logo.svg']);
  });

  it('downloads a diagram into the export folder when one is set', async () => {
    fakeRust('/Users/me/Guardian');
    await mount('#/debates/artifacts/thread_ab1/v2/logo.svg');
    await click(ko.artifacts.download);
    expect(saves()).toEqual([{ project: 'arch-docs', thread: 'thread_ab1', files: [{ relPath: 'v2/logo.svg', contents: 'text of logo.svg' }] }]);
    expect(screen.getByText(ko.artifacts.downloaded('/Users/me/Guardian/arch-docs/thread_ab1/v2/logo.svg'))).toBeTruthy();
  });

  it('still refuses a diagram download without a folder, and says how to fix it', async () => {
    fakeRust(null);
    await mount('#/debates/artifacts/thread_ab1/v2/logo.svg');
    await click(ko.artifacts.download);
    expect(saves()).toEqual([]);
    expect(screen.getByText(ko.artifacts.downloadFailed(ko.artifacts.desktopMarkdownOnly))).toBeTruthy();
  });

  it('keeps the Downloads folder for a Markdown file when no folder is set', async () => {
    fakeRust(null);
    invoke.mockImplementation(async (cmd: string) => (cmd === 'export_get_root' ? null : cmd === 'save_download' ? '/Users/me/Downloads/design.md' : undefined));
    await mount('#/debates/artifacts/thread_ab1/v2/design.md');
    await click(ko.artifacts.download);
    expect(invoke).toHaveBeenCalledWith('save_download', { fileName: 'design.md', contents: 'text of design.md' });
  });
});

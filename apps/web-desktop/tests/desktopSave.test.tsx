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
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);

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

describe('Artifacts in the desktop shell (no export folder since the dashboard review, X3)', () => {
  beforeEach(() => {
    window.localStorage.setItem('guardian.project', 'arch-docs');
    mocked.fetchProjectArtifacts.mockResolvedValue({ project: 'arch-docs', threads: [] });
  });

  it('refuses a diagram download and says why', async () => {
    await mount('#/debates/artifacts/thread_ab1/v2/logo.svg');
    await click(ko.artifacts.download);
    expect(invoke).not.toHaveBeenCalledWith('save_download', expect.anything());
    expect(screen.getByText(ko.artifacts.downloadFailed(ko.artifacts.desktopMarkdownOnly))).toBeTruthy();
  });

  it('writes a Markdown file to Downloads', async () => {
    invoke.mockImplementation(async (cmd: string) => (cmd === 'save_download' ? '/Users/me/Downloads/design.md' : undefined));
    await mount('#/debates/artifacts/thread_ab1/v2/design.md');
    await click(ko.artifacts.download);
    expect(invoke).toHaveBeenCalledWith('save_download', { fileName: 'design.md', contents: 'text of design.md' });
  });

  it('offers no save-to-folder button on a version', async () => {
    await mount('#/debates/artifacts/thread_ab1/v2');
    expect(screen.queryByRole('button', { name: /폴더/ })).toBeNull();
  });
});

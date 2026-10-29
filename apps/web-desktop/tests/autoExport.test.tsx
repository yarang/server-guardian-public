import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { ProjectEntry, ResultItem, ThreadReport } from '../src/types';

const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { isEligible, runAutoExport } from '../src/lib/autoExport';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);
const t = ko.shell.exportFolder;

const SINCE = Date.parse('2026-10-20T00:00:00Z');
const item = (thread_id: string, extra: Partial<ResultItem> = {}): ResultItem => ({
  thread_id, title: thread_id, status: 'APPROVED', rounds: 1, updated_at: '2026-10-21T09:00:00Z',
  has_report: true, report_bytes: 900, artifact_versions: 0, artifact_files: 0, ...extra,
});
const report = (thread: string): ThreadReport => ({
  thread_id: thread, project: 'server-guardian', markdown: `# ${thread}\n`, bytes: 10, updated_at: '2026-10-21T09:00:00Z', assets: [],
});
const entry = (id: string): ProjectEntry =>
  ({ id, source: 'custom', data: { id, name: id }, errors: [], status: { present: true } }) as ProjectEntry;

/** What the Rust side answers: the switch, the markers it keeps and the outcome of a save. */
function fakeRust(opts: { enabled?: boolean; marks?: Record<string, string>; outcome?: string } = {}) {
  invoke.mockImplementation(async (cmd: string, args?: { files?: unknown[] }) => {
    if (cmd === 'export_auto_get') return { enabled: opts.enabled ?? true, sinceMs: SINCE };
    if (cmd === 'export_auto_marks') return opts.marks ?? {};
    if (cmd === 'export_auto_recent') return [];
    if (cmd === 'export_auto_save') return (args?.files ?? []).map((_, i) => ({ relPath: `f${i}`, outcome: opts.outcome ?? 'written', name: `f${i}` }));
    if (cmd === 'export_reveal') return undefined;
    throw new Error(`unexpected command ${cmd}`);
  });
}

const saves = () => invoke.mock.calls.filter((c) => c[0] === 'export_auto_save');

beforeEach(() => {
  mocked.fetchThreadReport.mockImplementation(async (thread: string) => report(thread));
});
afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  invoke.mockReset();
  vi.clearAllMocks();
});

describe('isEligible', () => {
  const none = {};
  it('takes approved and disputed results only, in any spelling of the status word', () => {
    expect(isEligible(item('a'), none, SINCE)).toBe(true);
    expect(isEligible(item('a', { status: 'disputed' }), none, SINCE)).toBe(true);
    for (const status of ['', 'IN_PROGRESS', 'UNVERIFIED', 'REVISION_REQUIRED', 'failed']) {
      expect(isEligible(item('a', { status }), none, SINCE)).toBe(false);
    }
  });

  it('skips results older than the switch (no back-fill) and results without a usable date', () => {
    expect(isEligible(item('a', { updated_at: '2026-10-19T23:59:59Z' }), none, SINCE)).toBe(false);
    expect(isEligible(item('a', { updated_at: '2026-10-20T00:00:00Z' }), none, SINCE)).toBe(true);
    expect(isEligible(item('a', { updated_at: '' }), none, SINCE)).toBe(false);
    expect(isEligible(item('a', { updated_at: 'garbage' }), none, SINCE)).toBe(false);
  });

  it('skips a result whose current updated_at was already exported, but not a changed one', () => {
    expect(isEligible(item('a'), { a: '2026-10-21T09:00:00Z' }, SINCE)).toBe(false);
    expect(isEligible(item('a', { updated_at: '2026-10-22T09:00:00Z' }), { a: '2026-10-21T09:00:00Z' }, SINCE)).toBe(true);
  });

  it('needs something to write', () => {
    expect(isEligible(item('a', { has_report: false }), none, SINCE)).toBe(false);
    expect(isEligible(item('a', { has_report: false, artifact_versions: 2 }), none, SINCE)).toBe(true);
  });
});

describe('runAutoExport', () => {
  it('does nothing while the switch is off', async () => {
    fakeRust({ enabled: false });
    expect(await runAutoExport(['server-guardian'], new Set())).toBeNull();
    expect(mocked.fetchProjectResults).not.toHaveBeenCalled();
    expect(saves()).toHaveLength(0);
  });

  it('exports only the eligible results and hands the updated_at to Rust as the marker', async () => {
    fakeRust({ marks: { done: '2026-10-21T09:00:00Z' } });
    mocked.fetchProjectResults.mockResolvedValue({
      project: 'server-guardian',
      total: 4,
      items: [item('new'), item('done'), item('running', { status: 'IN_PROGRESS' }), item('old', { updated_at: '2026-10-01T00:00:00Z' })],
    });
    const run = await runAutoExport(['server-guardian'], new Set());
    expect(saves()).toHaveLength(1);
    expect(saves()[0][1]).toMatchObject({ project: 'server-guardian', thread: 'new', marker: '2026-10-21T09:00:00Z' });
    expect(run).toMatchObject({ changedThreads: 1, failedThreads: 0, counts: { written: 1, updated: 0 } });
  });

  it('counts in-place updates and writes no notice-worthy change for skipped files', async () => {
    fakeRust({ outcome: 'updated' });
    mocked.fetchProjectResults.mockResolvedValue({ project: 'server-guardian', total: 1, items: [item('a')] });
    expect(await runAutoExport(['server-guardian'], new Set())).toMatchObject({ changedThreads: 1, counts: { updated: 1 } });

    fakeRust({ outcome: 'skipped' });
    expect(await runAutoExport(['server-guardian'], new Set())).toMatchObject({ changedThreads: 0, counts: { skipped: 1 } });
  });

  it('does not retry a failed result until its updated_at changes', async () => {
    fakeRust();
    mocked.fetchThreadReport.mockRejectedValue(new Error('hub says no'));
    mocked.fetchProjectResults.mockResolvedValue({ project: 'server-guardian', total: 1, items: [item('a')] });
    const failed = new Set<string>();
    expect(await runAutoExport(['server-guardian'], failed)).toMatchObject({ failedThreads: 1, changedThreads: 0 });
    expect(await runAutoExport(['server-guardian'], failed)).toMatchObject({ failedThreads: 0 });
    expect(mocked.fetchThreadReport).toHaveBeenCalledTimes(1);

    mocked.fetchProjectResults.mockResolvedValue({ project: 'server-guardian', total: 1, items: [item('a', { updated_at: '2026-10-22T09:00:00Z' })] });
    await runAutoExport(['server-guardian'], failed);
    expect(mocked.fetchThreadReport).toHaveBeenCalledTimes(2);
    expect(saves()).toHaveLength(0);
  });
});

describe('AutoExport', () => {
  const mount = () =>
    render(
      <GuardianRoot>
        {/* AppStores mounts AutoExport itself, as in the app. */}
        <AppStores>
          <span />
        </AppStores>
      </GuardianRoot>
    );

  beforeEach(() => {
    mocked.fetchProjects.mockResolvedValue({ projects: [entry('server-guardian')], custom_dir: '/x' });
    mocked.fetchProjectResults.mockResolvedValue({ project: 'server-guardian', total: 1, items: [item('a')] });
  });

  it('tells the operator what it wrote, with a way to open the folder', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    fakeRust();
    mount();
    expect(await screen.findByText(t.auto.noticeTitle)).toBeTruthy();
    expect(screen.getByText(new RegExp(t.auto.noticeThreads(1)))).toBeTruthy();
    screen.getByRole('button', { name: t.show }).click();
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('export_reveal'));
    screen.getByRole('button', { name: t.auto.dismiss }).click();
    await waitFor(() => expect(screen.queryByText(t.auto.noticeTitle)).toBeNull());
  });

  it('stays silent and asks Rust nothing outside the desktop shell', async () => {
    fakeRust();
    mount();
    await waitFor(() => expect(mocked.fetchProjects).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));
    expect(invoke).not.toHaveBeenCalled();
    expect(screen.queryByText(t.auto.noticeTitle)).toBeNull();
  });

  it('stays silent while the switch is off', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    fakeRust({ enabled: false });
    mount();
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('export_auto_get'));
    await new Promise((r) => setTimeout(r, 50));
    expect(saves()).toHaveLength(0);
    expect(screen.queryByText(t.auto.noticeTitle)).toBeNull();
  });
});

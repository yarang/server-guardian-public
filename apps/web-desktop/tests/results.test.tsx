import { act, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { ProjectEntry, ProjectResults, ResultItem, ThreadReport } from '../src/types';

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

const project = (id: string, kind?: 'design' | 'ops'): ProjectEntry =>
  ({ id, source: 'custom', data: { id, name: id, kind }, errors: [], status: { present: true } }) as ProjectEntry;

const item = (thread_id: string, extra: Partial<ResultItem> = {}): ResultItem => ({
  thread_id, title: `Title of ${thread_id}`, status: 'APPROVED', rounds: 2, updated_at: '2026-10-21T09:00:00Z',
  has_report: true, report_bytes: 900, artifact_versions: 0, artifact_files: 0, ...extra,
});

const list = (items: ResultItem[], total = items.length): ProjectResults => ({ project: 'p', items, total });

const report: ThreadReport = {
  thread_id: 'thread_a1', project: 'server-guardian', markdown: '# Report body\n\nDetails.\n', bytes: 20, updated_at: '2026-10-21T09:00:00Z', assets: [],
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-22T10:00:00Z'));
  window.localStorage.setItem('guardian.project', 'server-guardian');
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.fetchProjects.mockResolvedValue({
    projects: [project('server-guardian'), project('fleet', 'ops'), project('arch-docs', 'design')],
    custom_dir: '/x',
  });
  mocked.fetchProjectResults.mockResolvedValue(list([item('thread_a1'), item('thread_b2', { status: 'DISPUTED', rounds: 0 })]));
  mocked.fetchThreadReport.mockResolvedValue(report);
});
afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

describe('Results tab', () => {
  it.each([
    ['code', 'server-guardian'],
    ['ops', 'fleet'],
    ['design', 'arch-docs'],
  ])('is offered for a %s project', async (_kind, id) => {
    window.localStorage.setItem('guardian.project', id);
    await mount('#/debates');
    expect(screen.getByRole('button', { name: ko.debate.page.tabResults })).toBeTruthy();
  });

  it('lists the results of the selected project with status, rounds and links', async () => {
    await mount('#/debates/results');
    expect(mocked.fetchProjectResults).toHaveBeenCalledWith('server-guardian');
    const link = screen.getByRole('link', { name: ko.results.openThread('Title of thread_a1') });
    expect(link.getAttribute('href')).toBe('#/debates/results/thread_a1');
    expect(screen.getByText(ko.debate.decisionLabel.APPROVED)).toBeTruthy();
    expect(screen.getByText(ko.debate.decisionLabel.DISPUTED)).toBeTruthy();
    expect(screen.getByText(ko.results.rounds(2))).toBeTruthy();
  });

  it('says so when there are no results', async () => {
    mocked.fetchProjectResults.mockResolvedValue(list([]));
    await mount('#/debates/results');
    expect(screen.getByText(ko.results.empty)).toBeTruthy();
  });

  it('tells when the list is cut off', async () => {
    mocked.fetchProjectResults.mockResolvedValue(list([item('thread_a1')], 250));
    await mount('#/debates/results');
    expect(screen.getByText(ko.results.capped(1, 250))).toBeTruthy();
  });

  it('opens one report inline', async () => {
    await mount('#/debates/results/thread_a1');
    expect(mocked.fetchThreadReport).toHaveBeenCalledWith('thread_a1');
    await settle(100);
    expect(screen.getByRole('heading', { name: 'Report body' })).toBeTruthy();
    expect(screen.getByRole('link', { name: ko.results.backToList }).getAttribute('href')).toBe('#/debates/results');
  });

  it('offers "keep as document" for a code or ops report, never for a design project', async () => {
    await mount('#/debates/results/thread_a1');
    await settle(100);
    expect(screen.getByRole('button', { name: ko.debate.report.keep })).toBeTruthy();
  });

  it('leaves "keep as document" out for a design project', async () => {
    window.localStorage.setItem('guardian.project', 'arch-docs');
    mocked.fetchThreadReport.mockResolvedValue({ ...report, project: 'arch-docs' });
    mocked.fetchProjectResults.mockResolvedValue(list([item('thread_a1')]));
    await mount('#/debates/results/thread_a1');
    await settle(100);
    expect(screen.getByRole('heading', { name: 'Report body' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: ko.debate.report.keep })).toBeNull();
  });

  it('does not show a report that belongs to another project', async () => {
    mocked.fetchThreadReport.mockResolvedValue({ ...report, project: 'fleet' });
    await mount('#/debates/results/thread_a1');
    await settle(100);
    expect(screen.getByText(ko.debate.report.otherProject)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Report body' })).toBeNull();
  });

  it('labels a not-approved decision with the shared decision catalog', async () => {
    mocked.fetchProjectResults.mockResolvedValue(
      list([item('thread_a1', { status: 'REVISION_REQUIRED' }), item('thread_b2', { status: 'SOMETHING_NEW' })])
    );
    await mount('#/debates/results');
    const chip = screen.getByTitle('REVISION_REQUIRED');
    expect(chip.textContent).toBe(ko.debate.decisionLabel['NOT APPROVED']);
    expect(screen.getByTitle('SOMETHING_NEW').textContent).toBe('SOMETHING_NEW');
  });

  it('links to the artifacts of a design project, and shows a thread without a report as artifacts only', async () => {
    window.localStorage.setItem('guardian.project', 'arch-docs');
    mocked.fetchProjectResults.mockResolvedValue(
      list([item('thread_a1', { has_report: false, artifact_versions: 2, artifact_files: 3, status: '', rounds: 0 })])
    );
    await mount('#/debates/results/thread_a1');
    expect(screen.getByRole('link', { name: ko.results.openArtifacts }).getAttribute('href')).toBe('#/debates/artifacts/thread_a1');
    expect(screen.getByText(ko.results.reportMissing)).toBeTruthy();
    expect(mocked.fetchThreadReport).not.toHaveBeenCalled();
  });

  it('shows a failed load with the gate retry instead of an empty list', async () => {
    mocked.fetchProjectResults.mockRejectedValue(new Error('boom'));
    await mount('#/debates/results');
    expect(screen.queryByText(ko.results.empty)).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain('boom');
  });

  it('does not treat "results" as a debate id', async () => {
    await mount('#/debates/results');
    expect(mocked.fetchDebate).not.toHaveBeenCalled();
  });
});

import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { doneJob } from './debateFixtures';
import { ko } from '../src/i18n/ko';
import type { DebateEvent, DebateJob, WikiList, WikiPageDetail } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { follow } from './nav';

const mocked = vi.mocked(api);
const originalMatchMedia = window.matchMedia;

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

function mockNarrow(narrow: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: narrow,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    onchange: null,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

/** The page the way App renders it: route params become props. */
function Routed() {
  const { params } = useRoute();
  return <DebatesPage jobId={params[0]} wikiPage={params[1]} />;
}

async function mount(hash = '#/debates/wiki') {
  window.location.hash = hash;
  const view = render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle(300);
  return view;
}

/** Page actions sit behind one button; open it before using Rewrite, Edit or Delete. */
function openActions() {
  fireEvent.click(screen.getByRole('button', { name: ko.wiki.viewer.pageActions }));
}

const llm = { kind: 'llm', provider: 'claude', job: 'wiki_1' };
const human = { kind: 'human', user: 'alice' };

const pages: WikiList = {
  project: 'server-guardian',
  sha: 'abcdef1234567',
  errors: [{ id: 'broken-file', error: 'front matter: unterminated' }],
  pages: [
    { id: 'apps-agent', title: 'Hub agent (apps/agent)', scope: ['apps/agent/'], author: llm, updated: '2026-10-12T09:00:00Z', state: 'fresh' },
    { id: 'cli-llm-agent', title: 'Debate engine', scope: ['cli/llm-agent/'], author: human, updated: '2026-10-11T09:00:00Z', state: 'stale' },
    { id: 'scripts', title: 'Scripts', scope: ['scripts/'], author: llm, updated: '2026-10-10T09:00:00Z', state: 'broken' },
  ],
};

const staleDetail: WikiPageDetail = {
  project: 'server-guardian',
  sha: 'abcdef1234567',
  page: { id: 'cli-llm-agent', title: 'Debate engine', scope: ['cli/llm-agent/'], author: human, updated: '2026-10-11T09:00:00Z', written_sha: '1111111222222' },
  narrative: '## Role\nRuns debates (cli/llm-agent/debate_engine.py:845).',
  state: 'stale',
  staleness: {
    state: 'stale', sha: 'abcdef1234567', written_sha: '1111111222222',
    changed: ['cli/llm-agent/debate_engine.py'], removed: ['cli/llm-agent/old.py'], new: ['cli/llm-agent/code_wiki.py'],
    broken: [{ citation: 'cli/llm-agent/gone.py:3', reason: 'path not found' }], unverified: [], notes: ['written commit unavailable'],
  },
  generated: 'cli/llm-agent/\n  debate_engine.py  class DebateEngine',
};
const freshDetail: WikiPageDetail = {
  ...staleDetail,
  page: { ...staleDetail.page, id: 'apps-agent', title: 'Hub agent (apps/agent)', author: llm, scope: ['apps/agent/'] },
  state: 'fresh',
  staleness: { ...staleDetail.staleness, state: 'fresh', changed: [], removed: [], new: [], broken: [], notes: [] },
};

const wikiJob: DebateJob = {
  id: 'dbt_20261012_090000_aaaaaa', task: 'code wiki build: stale pages', team: '', thread_id: '', rounds: 0, status: 'running',
  started_at: '2026-10-12T09:00:00Z', finished_at: null, exit_code: null, error: '', project: 'server-guardian', kind: 'code_wiki', events: true,
};

beforeEach(() => {
  for (const fn of [
    mocked.fetchWikiPages, mocked.fetchWikiPage, mocked.saveWikiPage, mocked.deleteWikiPage, mocked.startWikiBuild, mocked.fetchDebates,
    mocked.fetchDebate, mocked.pollDebateEvents, mocked.streamDebateEvents,
  ]) {
    fn.mockReset();
  }
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-12T10:00:00Z'));
  mockNarrow(false);
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.fetchWikiPages.mockResolvedValue(pages);
  mocked.fetchWikiPage.mockImplementation(async (_p, id) => (id === 'apps-agent' ? freshDetail : staleDetail));
  mocked.startWikiBuild.mockResolvedValue(wikiJob);
  mocked.pollDebateEvents.mockResolvedValue({ events: [], next: 0, done: true });
  mocked.streamDebateEvents.mockResolvedValue({ ended: true, lastSeq: 0 });
});
afterEach(() => {
  window.matchMedia = originalMatchMedia;
  window.location.hash = '';
  vi.useRealTimers();
});

describe('Wiki view: page list', () => {
  it('opens at the index: attention first, then a card for every root page, with the checked commit', async () => {
    await mount();
    expect(mocked.fetchWikiPages).toHaveBeenCalledWith('server-guardian');
    // No tree on the index; no page was asked for.
    expect(screen.queryByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeNull();
    expect(mocked.fetchWikiPage).not.toHaveBeenCalled();
    expect(screen.getByText(ko.wiki.tab.pageUnreadable('broken-file', 'front matter: unterminated'))).toBeTruthy();
    expect(screen.getByText('abcdef1')).toBeTruthy();
    expect(screen.getByText((t) => t.includes(ko.wiki.viewer.summaryLine(3, 2)))).toBeTruthy();

    const attention = screen.getByRole('region', { name: ko.wiki.viewer.attentionTitle });
    expect(within(attention).getByText(ko.wiki.stateLabel.stale)).toBeTruthy();
    expect(within(attention).getByText(ko.wiki.stateLabel.broken)).toBeTruthy();
    expect(within(attention).queryByText('Hub agent (apps/agent)')).toBeNull(); // fresh pages are not listed

    const start = screen.getByRole('region', { name: ko.wiki.viewer.startTitle });
    const rows = within(start).getAllByRole('link');
    expect(rows.map((r) => r.getAttribute('aria-label'))).toEqual(['apps-agent', 'cli-llm-agent', 'scripts'].map(ko.wiki.tab.openPage));
    expect(rows[0].getAttribute('href')).toBe('#/debates/wiki/apps-agent');
    expect(within(start).getByText('apps/agent/')).toBeTruthy();
  });

  it('filters the pages by title, id and scope, and says when nothing matches', async () => {
    await mount();
    const box = screen.getByLabelText(ko.wiki.viewer.searchLabel);
    fireEvent.change(box, { target: { value: 'DEBATE' } });
    expect(screen.getByText(ko.wiki.viewer.resultsTitle(1))).toBeTruthy();
    expect(screen.getAllByRole('link', { name: ko.wiki.tab.openPage('cli-llm-agent') })).toHaveLength(1);
    expect(screen.queryByRole('link', { name: ko.wiki.tab.openPage('apps-agent') })).toBeNull();
    expect(screen.queryByRole('region', { name: ko.wiki.viewer.startTitle })).toBeNull();

    fireEvent.change(box, { target: { value: 'scripts/' } }); // the scope of a page
    expect(screen.getByRole('link', { name: ko.wiki.tab.openPage('scripts') })).toBeTruthy();
    fireEvent.change(box, { target: { value: 'zzz' } });
    expect(screen.getByText(ko.wiki.viewer.noMatch)).toBeTruthy();
    fireEvent.change(box, { target: { value: '' } });
    expect(screen.getByRole('region', { name: ko.wiki.viewer.startTitle })).toBeTruthy();
  });

  it('switches between Debates and Wiki with the section control', async () => {
    await mount();
    fireEvent.click(within(screen.getByRole('group', { name: ko.debate.page.sections })).getByRole('button', { name: ko.debate.page.tabDebates }));
    await settle(300);
    expect(window.location.hash).toBe('#/debates');
    expect(screen.queryByLabelText(ko.wiki.viewer.searchLabel)).toBeNull();
    fireEvent.click(within(screen.getByRole('group', { name: ko.debate.page.sections })).getByRole('button', { name: ko.debate.page.tabWiki }));
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki');
    expect(screen.getByLabelText(ko.wiki.viewer.searchLabel)).toBeTruthy();
  });

  it('offers to write the first pages when the project has none', async () => {
    mocked.fetchWikiPages.mockResolvedValue({ ...pages, pages: [], errors: [] });
    await mount();
    expect(screen.getByText(ko.wiki.tab.empty)).toBeTruthy();
    expect(screen.queryByRole('button', { name: ko.wiki.tab.writeFirst })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.modes.history }));
    await settle(300);
    expect(screen.queryByRole('button', { name: /^오래된 페이지 다시 쓰기/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.writeFirst }));
    await settle();
    expect(mocked.startWikiBuild).toHaveBeenCalledWith('server-guardian', {});
  });

  it('starts a build for the stale pages and links to the monitor', async () => {
    await mount('#/debates/wiki?view=history');
    // Two of three pages are not fresh.
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.rewriteStale(2) }));
    await settle();
    expect(mocked.startWikiBuild).toHaveBeenCalledWith('server-guardian', { stale: true });
    const link = screen.getByRole('link', { name: ko.wiki.tab.followInMonitor });
    expect(link.getAttribute('href')).toBe('#/debates/dbt_20261012_090000_aaaaaa');
  });

  it('asks before rewriting every page, then sends all', async () => {
    await mount('#/debates/wiki?view=history');
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.rewriteAll }));
    expect(mocked.startWikiBuild).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.common.cancel }));
    expect(mocked.startWikiBuild).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.rewriteAll }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.wiki.tab.rewriteAll }));
    await settle();
    expect(mocked.startWikiBuild).toHaveBeenCalledWith('server-guardian', { all: true });
  });

  it('shows the hub message when a build cannot start', async () => {
    mocked.startWikiBuild.mockRejectedValue(new Error('a code wiki build is already running for this project; wait for it to finish or cancel it'));
    await mount('#/debates/wiki?view=history');
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.rewriteStale(2) }));
    await settle();
    expect(screen.getAllByRole('alert').map((a) => a.textContent).join('\n')).toContain(ko.wiki.tab.buildFailed('a code wiki build is already running'));
  });

  it('disables the build buttons while a build of this project runs and reloads the list when it ends', async () => {
    mocked.fetchDebates.mockResolvedValue([wikiJob]);
    await mount('#/debates/wiki?view=history');
    expect((screen.getByRole('button', { name: ko.wiki.tab.rewriteStale(2) }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: ko.wiki.tab.rewriteAll }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('link', { name: ko.wiki.tab.followInMonitor }).getAttribute('href')).toBe(`#/debates/${wikiJob.id}`);
    const calls = mocked.fetchWikiPages.mock.calls.length;

    mocked.fetchDebates.mockResolvedValue([{ ...wikiJob, status: 'completed', finished_at: '2026-10-12T09:10:00Z', exit_code: 0 }]);
    await settle(6000);
    expect(mocked.fetchWikiPages.mock.calls.length).toBeGreaterThan(calls);
    expect((screen.getByRole('button', { name: ko.wiki.tab.rewriteStale(2) }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('Wiki view: one page', () => {
  it('keeps the narrative and the generated overview apart and explains why the page is stale', async () => {
    await mount('#/debates/wiki/cli-llm-agent');
    expect(mocked.fetchWikiPage).toHaveBeenCalledWith('server-guardian', 'cli-llm-agent');
    expect(screen.getByRole('link', { name: ko.wiki.tab.openPage('cli-llm-agent') }).getAttribute('aria-current')).toBe('page');

    // Staleness details.
    expect(screen.getByText(/페이지를 쓴 뒤 파일이 바뀌었어요/)).toBeTruthy();
    expect(screen.getByText(/- cli\/llm-agent\/debate_engine\.py/)).toBeTruthy();
    expect(screen.getByText(/- cli\/llm-agent\/old\.py/)).toBeTruthy();
    expect(screen.getByText(/- cli\/llm-agent\/code_wiki\.py/)).toBeTruthy();
    expect(screen.getByText(/cli\/llm-agent\/gone\.py:3 \(path not found\)/)).toBeTruthy();
    expect(screen.getByText('written commit unavailable')).toBeTruthy();

    // Two separate sections: the written text (rendered Markdown) and the generated map.
    expect(screen.getByText(/서술 \(작성: alice \(사람\)\)/)).toBeTruthy();
    expect(screen.getByText(/자동 생성 개요: 페이지를 열 때 코드에서 만들며/)).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Role' })).toBeTruthy();
    const narrative = screen.getByText(/Runs debates/);
    expect(narrative.tagName).toBe('P');
    expect(narrative.textContent).toBe('Runs debatesdebate_engine.py:845.');
    const cite = within(narrative).getByText('debate_engine.py:845');
    expect(cite.getAttribute('title')).toBe('cli/llm-agent/debate_engine.py:845');
    // The chip opens the file view at the cited line.
    expect(cite.tagName).toBe('A');
    expect(cite.getAttribute('href')).toBe('#/debates/wiki?file=cli%2Fllm-agent%2Fdebate_engine.py&lines=845');
    // The trailing citation can be hidden; the choice is remembered.
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.hideCitations }));
    expect(narrative.closest('article')?.className).toBe('sg-cites-off');
    expect(screen.getByRole('button', { name: ko.wiki.page.showCitations(1) })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.showCitations(1) }));
    expect(narrative.closest('article')?.className).toBe('');
    const generated = screen.getByText(/class DebateEngine/);
    expect(generated.tagName).toBe('PRE');
    expect(generated.closest('details')).not.toBe(narrative.closest('details'));
    expect(screen.getByText(ko.wiki.page.repoMap('abcdef1'))).toBeTruthy();
  });

  it('shows no warning for a fresh page', async () => {
    await mount('#/debates/wiki/apps-agent');
    expect(screen.queryByText(/페이지를 쓴 뒤 파일이 바뀌었어요/)).toBeNull();
    expect(screen.getAllByText(ko.wiki.stateLabel.fresh).length).toBeGreaterThan(0);
  });

  it('edits the page text, title and scope and saves them', async () => {
    mocked.saveWikiPage.mockResolvedValue({ page: staleDetail.page, state: 'fresh', staleness: staleDetail.staleness });
    await mount('#/debates/wiki/cli-llm-agent');
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.editAria('cli-llm-agent') }));
    const dialog = screen.getByRole('dialog');
    expect((within(dialog).getByLabelText(ko.wiki.edit.pageId) as HTMLInputElement).readOnly).toBe(true);
    expect((within(dialog).getByLabelText(ko.wiki.edit.title) as HTMLInputElement).value).toBe('Debate engine');
    expect((within(dialog).getByLabelText(/^범위/) as HTMLTextAreaElement).value).toBe('cli/llm-agent/');
    expect((within(dialog).getByLabelText(ko.wiki.edit.body) as HTMLTextAreaElement).value).toBe(staleDetail.narrative);

    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.title), { target: { value: 'Debate engine v2' } });
    fireEvent.change(within(dialog).getByLabelText(/^범위/), { target: { value: 'cli/llm-agent/\n docs/design/ ' } });
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.body), { target: { value: '## Role\nNew text.' } });
    const loads = mocked.fetchWikiPage.mock.calls.length;
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await settle();
    expect(mocked.saveWikiPage).toHaveBeenCalledWith('server-guardian', 'cli-llm-agent', {
      title: 'Debate engine v2', scope: ['cli/llm-agent/', 'docs/design/'], body: '## Role\nNew text.',
    });
    expect(screen.queryByRole('dialog')).toBeNull();
    // The page and the list are read again.
    expect(mocked.fetchWikiPage.mock.calls.length).toBeGreaterThan(loads);
  });

  it('validates the edit before calling the hub and shows the hub error', async () => {
    mocked.saveWikiPage.mockRejectedValue(new Error('page text too large'));
    await mount('#/debates/wiki/cli-llm-agent');
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.editAria('cli-llm-agent') }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.title), { target: { value: '  ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    expect(within(dialog).getByRole('alert').textContent).toBe(ko.wiki.edit.titleRequired);
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.title), { target: { value: 'T' } });
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.body), { target: { value: 'x'.repeat(256 * 1024 + 1) } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    expect(within(dialog).getByRole('alert').textContent).toContain(ko.wiki.edit.tooLarge(256, 256));
    expect(mocked.saveWikiPage).not.toHaveBeenCalled();
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.body), { target: { value: 'ok' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await settle();
    expect(within(dialog).getByRole('alert').textContent).toBe('page text too large');
  });

  it('creates a new page by hand with a validated id', async () => {
    mocked.saveWikiPage.mockResolvedValue({ page: staleDetail.page, state: 'fresh', staleness: staleDetail.staleness });
    await mount('#/debates/wiki?view=history');
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.newPage }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.pageId), { target: { value: 'Bad Id' } });
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.title), { target: { value: 'Notes' } });
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.body), { target: { value: 'hello' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    expect(within(dialog).getByRole('alert').textContent).toContain(ko.wiki.edit.pageIdInvalid);
    fireEvent.change(within(dialog).getByLabelText(ko.wiki.edit.pageId), { target: { value: 'notes' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await settle();
    expect(mocked.saveWikiPage).toHaveBeenCalledWith('server-guardian', 'notes', { title: 'Notes', scope: [], body: 'hello' });
    expect(window.location.hash).toBe('#/debates/wiki/notes');
  });

  it('deletes a page only after confirmation', async () => {
    mocked.deleteWikiPage.mockResolvedValue(undefined);
    await mount('#/debates/wiki/cli-llm-agent');
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.deleteAria('cli-llm-agent') }));
    expect(mocked.deleteWikiPage).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain('사람이 쓴 이 글은 사라져요');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.wiki.page.deleteLabel }));
    await settle();
    expect(mocked.deleteWikiPage).toHaveBeenCalledWith('server-guardian', 'cli-llm-agent');
    expect(window.location.hash).toBe('#/debates/wiki');
  });

  it('shows a failed delete inside the dialog', async () => {
    mocked.deleteWikiPage.mockRejectedValue(new Error('page not found'));
    await mount('#/debates/wiki/cli-llm-agent');
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.deleteAria('cli-llm-agent') }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.wiki.page.deleteLabel }));
    await settle();
    expect(within(screen.getByRole('dialog')).getByRole('alert').textContent).toBe('page not found');
  });

  it('rewrites a written page directly and asks (then forces) for a page written by a person', async () => {
    await mount('#/debates/wiki/apps-agent');
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.rewriteAria('apps-agent') }));
    await settle();
    expect(mocked.startWikiBuild).toHaveBeenLastCalledWith('server-guardian', { pages: ['apps-agent'] });

    follow(screen.getByRole('link', { name: ko.wiki.tab.openPage('cli-llm-agent') }));
    await settle(300);
    mocked.startWikiBuild.mockClear();
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.rewriteAria('cli-llm-agent') }));
    expect(mocked.startWikiBuild).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog').textContent).toContain(ko.wiki.page.rewriteMessage('cli-llm-agent', ko.wiki.author.person('alice')));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.wiki.page.rewriteLabel }));
    await settle();
    expect(mocked.startWikiBuild).toHaveBeenCalledWith('server-guardian', { pages: ['cli-llm-agent'], force: true });
  });

  it('reports a page that cannot be loaded', async () => {
    mocked.fetchWikiPage.mockRejectedValue(new Error('page not found'));
    await mount('#/debates/wiki/gone');
    expect(screen.getByText(ko.shell.dataGate.couldNotLoad(ko.wiki.page.what('gone')))).toBeTruthy();
    expect(screen.getByText('page not found')).toBeTruthy();
  });
});

describe('Wiki view: narrow screens', () => {
  it('shows the index alone, then the page alone with a way back', async () => {
    mockNarrow(true);
    await mount();
    expect(screen.getByLabelText(ko.wiki.viewer.searchLabel)).toBeTruthy();
    expect(mocked.fetchWikiPage).not.toHaveBeenCalled();

    follow(screen.getByRole('link', { name: ko.wiki.tab.openPage('cli-llm-agent') }));
    await settle(300);
    // No tree, no index controls: the page, with the place bar on top.
    expect(screen.queryByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeNull();
    expect(screen.queryByLabelText(ko.wiki.viewer.searchLabel)).toBeNull();
    expect(screen.getByText(/페이지를 쓴 뒤 파일이 바뀌었어요/)).toBeTruthy();
    follow(screen.getByRole('link', { name: ko.wiki.tab.backToPagesAria }));
    await settle(300);
    expect(screen.getByLabelText(ko.wiki.viewer.searchLabel)).toBeTruthy();
    expect(screen.queryByText(/페이지를 쓴 뒤 파일이 바뀌었어요/)).toBeNull();
  });
});

describe('Agents monitor: code wiki build jobs', () => {
  const writer = { round: 1, role: 'Domain Expert', agent: 'expert', agent_name: 'Expert' };
  const t = (s: number) => new Date(Date.parse('2026-10-12T09:00:00Z') + s * 1000).toISOString();
  const events = [
    { seq: 1, ts: t(0), type: 'wiki_build_started', data: { project: 'server-guardian', team: 'default', sha: 'abcdef1234567', pages: ['apps-agent', 'scripts'], language: 'Korean', force: false } },
    { seq: 2, ts: t(1), type: 'compiler_index', data: { lang: 'go', sha: 'abcdef1234567', status: 'built', packages: 3, defs: 50, refs: 200, unresolved: 0, ms: 1500, error: null } },
    { seq: 3, ts: t(2), type: 'agent_started', ...writer, data: { prompt_bytes: 1000 } },
    { seq: 4, ts: t(3), type: 'retrieval_warning', ...writer, data: { step: 1, bytes: 3_000_000, threshold: 2_097_152, via: 'block' } },
    { seq: 5, ts: t(8), type: 'agent_finished', ...writer, data: { provider: 'claude', duration_ms: 6000, steps: 3 } },
    { seq: 6, ts: t(9), type: 'wiki_page_written', data: { page: 'apps-agent', state: 'fresh', basis_files: 4, citations: 3, unverified: [] } },
    { seq: 7, ts: t(10), type: 'wiki_page_skipped', data: { page: 'scripts', reason: 'llm_fallback: no provider answered' } },
    { seq: 8, ts: t(11), type: 'wiki_build_finished', data: { written: 1, skipped: 1, failed: 1 } },
  ] as DebateEvent[];

  it('lists a build with its kind and shows its pages and events in the monitor', async () => {
    const finished: DebateJob = { ...wikiJob, status: 'completed', finished_at: t(12), exit_code: 0 };
    mocked.fetchDebates.mockResolvedValue([finished, { ...doneJob, id: 'job2', events: true }]);
    mocked.fetchDebate.mockImplementation(async (id) => ({ ...(id === finished.id ? finished : doneJob), log: '', result: null }));
    mocked.pollDebateEvents.mockResolvedValue({ events, next: 8, done: true });
    await mount(`#/debates/${finished.id}`);

    const row = screen.getByRole('link', { name: ko.agents.list.openWiki(finished.id) });
    expect(within(row).getByText(ko.wiki.jobLabel)).toBeTruthy();
    expect(screen.getByRole('link', { name: ko.agents.list.openDebate('job2') })).toBeTruthy();

    // The monitor, not the debate result view, shows the finished build.
    expect(screen.queryByText('Agent activity: lanes, outcome and timeline')).toBeNull();
    expect(mocked.fetchDebate).toHaveBeenCalledWith(finished.id);
    expect(screen.getByRole('link', { name: ko.agents.detail.openWiki }).getAttribute('href')).toBe('#/debates/wiki');
    const pagesCard = screen.getByText(ko.agents.detail.pages(2, 2)).parentElement as HTMLElement;
    expect(within(pagesCard).getByText('apps-agent')).toBeTruthy();
    expect(within(pagesCard).getByText(ko.wiki.stateLabel.fresh)).toBeTruthy();
    expect(within(pagesCard).getByText(ko.agents.detail.skipped)).toBeTruthy();

    const log = screen.getByRole('log', { name: ko.agents.timeline.aria });
    expect(within(log).getByText(ko.agents.timeline.wikiStarted)).toBeTruthy();
    expect(within(log).getByText(ko.agents.timeline.compilerIndex('GO'))).toBeTruthy();
    expect(within(log).getByText(ko.agents.indexStatus.BUILT)).toBeTruthy();
    expect(log.textContent).toContain(ko.agents.timeline.retrievalWarning('Expert', 1, '2929.7 KB', '2048.0 KB', false));
    expect(within(log).getByText(ko.agents.timeline.pageWritten)).toBeTruthy();
    expect(within(log).getByText(ko.agents.timeline.pageSkipped('scripts', 'llm_fallback: no provider answered'))).toBeTruthy();
    expect(within(log).getByText(ko.agents.timeline.wikiFinished)).toBeTruthy();
    expect(within(log).getByText(ko.agents.timeline.writtenCount(1))).toBeTruthy();
  });

  it('cancels a running build through the debate cancel route', async () => {
    mocked.fetchDebates.mockResolvedValue([wikiJob]);
    mocked.fetchDebate.mockResolvedValue({ ...wikiJob, log: '', result: null });
    mocked.pollDebateEvents.mockResolvedValue({ events: events.slice(0, 3), next: 3, done: false });
    mocked.streamDebateEvents.mockImplementation(() => new Promise(() => undefined));
    mocked.cancelDebate.mockResolvedValue(undefined);
    await mount(`#/debates/${wikiJob.id}`);
    fireEvent.click(screen.getByRole('button', { name: ko.common.cancel }));
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toContain(ko.shell.cancelDebate.messageWiki);
    fireEvent.click(within(dialog).getByRole('button', { name: ko.shell.cancelDebate.confirmWiki }));
    await settle();
    expect(mocked.cancelDebate).toHaveBeenCalledWith(wikiJob.id);
  });
});

describe('Wiki view: feature pages', () => {
  const featureList: WikiList = {
    ...pages,
    errors: [],
    pages: [{ id: 'feature-auth', title: 'Auth design', scope: ['docs/design/auth.md', 'svc/auth.go'], author: llm, updated: '2026-10-12T09:00:00Z', state: 'fresh', kind: 'feature' }],
  };
  const featureDetail: WikiPageDetail = {
    ...freshDetail,
    page: { ...freshDetail.page, id: 'feature-auth', title: 'Auth design', scope: ['docs/design/auth.md', 'svc/'], kind: 'feature', docs: ['docs/guide.md'] },
  };

  it('marks feature pages and starts a feature build after confirming', async () => {
    mocked.fetchWikiPages.mockResolvedValue(featureList);
    await mount();
    const row = screen.getByRole('link', { name: ko.wiki.tab.openPage('feature-auth') }).parentElement as HTMLElement;
    expect(within(screen.getByRole('region', { name: ko.wiki.viewer.featuresTitle })).getByText(ko.wiki.kindLabel.feature)).toBeTruthy();
    expect(row).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.modes.history }));
    await settle(300);
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.writeFeatures }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.wiki.tab.writeFeatures }));
    await settle(10);
    expect(mocked.startWikiBuild).toHaveBeenCalledWith('server-guardian', { kind: 'feature' });
  });

  it('links the scope files and the related documents to the file view', async () => {
    mocked.fetchWikiPages.mockResolvedValue(featureList);
    mocked.fetchWikiPage.mockResolvedValue(featureDetail);
    await mount('#/debates/wiki/feature-auth');
    expect(screen.getByRole('link', { name: 'docs/design/auth.md' }).getAttribute('href')).toBe('#/debates/wiki?file=docs%2Fdesign%2Fauth.md');
    expect(screen.getByRole('link', { name: 'docs/guide.md' }).getAttribute('href')).toBe('#/debates/wiki?file=docs%2Fguide.md');
    expect(screen.queryByRole('link', { name: 'svc/' })).toBeNull(); // a directory stays plain text
    expect(screen.getByText(ko.wiki.page.docs)).toBeTruthy();
  });
});

describe('Wiki view: split directories', () => {
  const split: WikiList = {
    ...pages,
    errors: [],
    pages: [
      { id: 'cli-llm-agent', title: 'CLI', scope: ['cli/llm-agent/'], author: llm, updated: '2026-10-12T09:00:00Z', state: 'fresh', kind: 'overview', summary: 'The debate engine and the code wiki.' },
      { id: 'cli-llm-agent--code-index', title: 'Index', scope: ['cli/llm-agent/code_index.py'], author: llm, updated: '2026-10-12T09:00:00Z', state: 'fresh', kind: 'group' },
    ],
  };

  it('shows the overview as a card with its summary and groups below it, and starts a group build after confirming', async () => {
    mocked.fetchWikiPages.mockResolvedValue(split);
    await mount();
    const cards = screen.getByRole('region', { name: ko.wiki.viewer.startTitle });
    // One card: the group is inside the overview, not a card of its own.
    expect(within(cards).getAllByRole('link', { hidden: true }).map((a) => a.getAttribute('aria-label'))).toEqual(
      ['cli-llm-agent', 'cli-llm-agent--code-index'].map(ko.wiki.tab.openPage)
    );
    expect(within(cards).getByText('The debate engine and the code wiki.')).toBeTruthy();
    expect(within(cards).getByText(ko.wiki.kindLabel.overview)).toBeTruthy();
    expect(within(cards).getByText(ko.wiki.viewer.groupsCount(1))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.modes.history }));
    await settle(300);
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.tab.writeGroups }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.wiki.tab.writeGroups }));
    await settle(10);
    expect(mocked.startWikiBuild).toHaveBeenCalledWith('server-guardian', { kind: 'group' });
  });

  it('indents groups in the tree, shows the path, the groups of an overview and previous and next', async () => {
    mocked.fetchWikiPages.mockResolvedValue(split);
    await mount('#/debates/wiki/cli-llm-agent');
    const tree = screen.getByRole('navigation', { name: ko.wiki.tab.pagesAria });
    const overview = within(tree).getByRole('link', { name: ko.wiki.tab.openPage('cli-llm-agent') });
    const group = within(tree).getByRole('link', { name: ko.wiki.tab.openPage('cli-llm-agent--code-index') });
    expect(overview.style.marginLeft).toBe('');
    expect(group.style.marginLeft).not.toBe('');
    expect(overview.getAttribute('aria-current')).toBe('page');

    // The overview lists its groups and leads to the first one.
    expect(screen.getByText(ko.wiki.viewer.groupsOf(1))).toBeTruthy();
    const next = screen.getByRole('link', { name: ko.wiki.viewer.nextAria('Index') });
    expect(next.getAttribute('href')).toBe('#/debates/wiki/cli-llm-agent--code-index');
    expect(screen.queryByRole('link', { name: ko.wiki.viewer.previousAria('CLI') })).toBeNull();

    follow(next);
    await settle(300);
    const place = screen.getByRole('navigation', { name: ko.wiki.viewer.placeAria });
    expect(within(place).getByRole('link', { name: ko.wiki.viewer.indexCrumb }).getAttribute('href')).toBe('#/debates/wiki');
    expect(within(place).getByRole('link', { name: 'CLI' }).getAttribute('href')).toBe('#/debates/wiki/cli-llm-agent');
    expect(place.querySelector('[aria-current="page"]')?.textContent).toBe('Index');
    expect(screen.getByRole('link', { name: ko.wiki.viewer.previousAria('CLI') })).toBeTruthy();
    expect(screen.queryByRole('link', { name: ko.wiki.viewer.nextAria('CLI') })).toBeNull();
  });
});

describe('Wiki view: reading', () => {
  const linked: WikiList = {
    ...pages,
    errors: [],
    pages: [
      { id: 'apps-agent', title: 'Hub agent', scope: ['apps/agent/'], author: llm, updated: '2026-10-12T09:00:00Z', state: 'fresh', links: ['cli-llm-agent'] },
      { id: 'cli-llm-agent', title: 'Debate engine', scope: ['cli/llm-agent/'], author: human, updated: '2026-10-11T09:00:00Z', state: 'fresh', links: [] },
      { id: 'scripts', title: 'Scripts', scope: ['scripts/'], author: llm, updated: '2026-10-10T09:00:00Z', state: 'fresh', links: ['cli-llm-agent'] },
    ],
  };

  it('turns [[id]] into a link, marks an unknown id, and lists the pages that link here', async () => {
    mocked.fetchWikiPages.mockResolvedValue(linked);
    mocked.fetchWikiPage.mockResolvedValue({ ...freshDetail, narrative: '## Related\nSee [[scripts]] and [[gone]].' });
    await mount('#/debates/wiki/cli-llm-agent');
    const body = screen.getByRole('article');
    expect(within(body).getByRole('link', { name: 'scripts' }).getAttribute('href')).toBe('#/debates/wiki/scripts');
    expect(within(body).queryByRole('link', { name: 'gone' })).toBeNull();
    expect(within(body).getByText('gone')).toBeTruthy();

    const footer = screen.getByText(ko.wiki.viewer.referencedBy).parentElement as HTMLElement;
    expect(within(footer).getAllByRole('link').map((a) => a.getAttribute('aria-label'))).toEqual(['apps-agent', 'scripts'].map(ko.wiki.tab.openPage));
    expect(screen.getByRole('link', { name: ko.wiki.viewer.previousAria('Hub agent') }).getAttribute('href')).toBe('#/debates/wiki/apps-agent');
    expect(screen.getByRole('link', { name: ko.wiki.viewer.nextAria('Scripts') }).getAttribute('href')).toBe('#/debates/wiki/scripts');
  });

  it('shows no "Referenced by" when the hub sends no links', async () => {
    await mount('#/debates/wiki/cli-llm-agent');
    expect(screen.queryByText(ko.wiki.viewer.referencedBy)).toBeNull();
  });

  it('keeps the page actions behind one button that closes on Escape and on a click outside', async () => {
    await mount('#/debates/wiki/apps-agent');
    expect(screen.queryByRole('button', { name: ko.wiki.page.deleteAria('apps-agent') })).toBeNull();
    openActions();
    expect(screen.getByRole('button', { name: ko.wiki.viewer.pageActions }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('button', { name: ko.wiki.page.rewriteAria('apps-agent') })).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('button', { name: ko.wiki.page.deleteAria('apps-agent') })).toBeNull();
    openActions();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('button', { name: ko.wiki.page.editAria('apps-agent') })).toBeNull();
  });

  it('still asks before deleting, and closes the menu when an action is chosen', async () => {
    mocked.deleteWikiPage.mockResolvedValue(undefined);
    await mount('#/debates/wiki/apps-agent');
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.deleteAria('apps-agent') }));
    expect(screen.queryByRole('button', { name: ko.wiki.page.editAria('apps-agent') })).toBeNull();
    expect(mocked.deleteWikiPage).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('disables Rewrite in the menu while a build runs', async () => {
    mocked.fetchDebates.mockResolvedValue([wikiJob]);
    await mount('#/debates/wiki/apps-agent');
    openActions();
    expect((screen.getByRole('button', { name: ko.wiki.page.rewriteAria('apps-agent') }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: ko.wiki.page.editAria('apps-agent') }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('remembers a collapsed tree', async () => {
    window.localStorage.removeItem('sg.wiki.tree.collapsed');
    const view = await mount('#/debates/wiki/apps-agent');
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.viewer.treeCollapse }));
    expect(screen.queryByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeNull();
    expect(window.localStorage.getItem('sg.wiki.tree.collapsed')).toBe('1');
    view.unmount();

    await mount('#/debates/wiki/apps-agent');
    expect(screen.queryByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.viewer.treeExpand }));
    expect(screen.getByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeTruthy();
    expect(window.localStorage.getItem('sg.wiki.tree.collapsed')).toBe('0');
    window.localStorage.removeItem('sg.wiki.tree.collapsed');
  });
});

describe('Wiki view: storage unavailable', () => {
  afterEach(() => vi.restoreAllMocks());

  it('reads the tree expanded and still toggles when localStorage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    await mount('#/debates/wiki/apps-agent');
    expect(screen.getByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.viewer.treeCollapse }));
    expect(screen.queryByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.viewer.treeExpand }));
    expect(screen.getByRole('navigation', { name: ko.wiki.tab.pagesAria })).toBeTruthy();
  });
});

describe('Wiki view: keyboard shortcuts', () => {
  const key = (code: string, init: KeyboardEventInit = {}, target: Element = document.body) =>
    fireEvent.keyDown(target, { code, key: code, ...init });

  it('moves to the previous and next page with [ and ], and to the index with g then i', async () => {
    await mount('#/debates/wiki/cli-llm-agent');
    key('BracketRight');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki/scripts');
    key('BracketLeft');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki/cli-llm-agent');
    key('KeyG');
    key('KeyI');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki');
  });

  it('does nothing at the ends of the reading order, and a lone g does not go anywhere', async () => {
    await mount('#/debates/wiki/scripts');
    key('BracketRight');
    key('KeyG');
    await settle(1100); // longer than the g-i window
    key('KeyI');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki/scripts');
  });

  it('ignores keys typed into a field, with a modifier, or while a dialog is open', async () => {
    const index = await mount('#/debates/wiki');
    const box = screen.getByLabelText(ko.wiki.viewer.searchLabel);
    key('Slash', {}, box);
    fireEvent.change(box, { target: { value: 'a' } });
    key('BracketRight', {}, box);
    expect(window.location.hash).toBe('#/debates/wiki');
    index.unmount();

    await mount('#/debates/wiki/cli-llm-agent');
    key('BracketRight', { ctrlKey: true });
    key('BracketRight', { metaKey: true });
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki/cli-llm-agent');

    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.deleteAria('cli-llm-agent') }));
    key('BracketRight');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki/cli-llm-agent');
  });

  it('focuses the search box on the index with /, and from a page goes to the index and focuses it', async () => {
    const index = await mount('#/debates/wiki');
    key('Slash');
    expect(document.activeElement).toBe(screen.getByLabelText(ko.wiki.viewer.searchLabel));
    index.unmount();

    await mount('#/debates/wiki/cli-llm-agent');
    key('Slash');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki?find=1');
    expect(document.activeElement).toBe(screen.getByLabelText(ko.wiki.viewer.searchLabel));
  });

  it('is off in the file view', async () => {
    mocked.fetchCodeFiles.mockReset();
    mocked.fetchCodeFiles.mockResolvedValue({ project: 'server-guardian', sha: 'abcdef1234567', files: [], truncated: false });
    await mount('#/debates/wiki?view=files');
    key('KeyG');
    key('KeyI');
    key('Slash');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki?view=files');
  });

  it('is off in the history view', async () => {
    await mount('#/debates/wiki?view=history');
    key('KeyG');
    key('KeyI');
    key('Slash');
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki?view=history');
  });
});

describe('Wiki view: modes', () => {
  const buildButtons = [ko.wiki.tab.rewriteAll, ko.wiki.tab.writeFeatures, ko.wiki.tab.writeGroups, ko.wiki.tab.newPage, ko.wiki.tab.reload];
  const modeButton = (name: string) => within(screen.getByRole('group', { name: ko.wiki.modes.aria })).getByRole('button', { name });

  it('reads, browses files and shows the history from one control, and keeps the query in the address', async () => {
    mocked.fetchCodeFiles.mockReset();
    mocked.fetchCodeFiles.mockResolvedValue({ project: 'server-guardian', sha: 'abcdef1234567', files: [], truncated: false });
    await mount();
    expect(modeButton(ko.wiki.modes.read).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(modeButton(ko.wiki.modes.files));
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki?view=files');
    expect(modeButton(ko.wiki.modes.files).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(modeButton(ko.wiki.modes.history));
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki?view=history');
    expect(modeButton(ko.wiki.modes.history).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(modeButton(ko.wiki.modes.read));
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki');
    expect(screen.getByLabelText(ko.wiki.viewer.searchLabel)).toBeTruthy();
  });

  it('has no build buttons while reading the index, a page or the files', async () => {
    mocked.fetchCodeFiles.mockReset();
    mocked.fetchCodeFiles.mockResolvedValue({ project: 'server-guardian', sha: 'abcdef1234567', files: [], truncated: false });
    for (const hash of ['#/debates/wiki', '#/debates/wiki/cli-llm-agent', '#/debates/wiki?view=files']) {
      const view = await mount(hash);
      for (const name of buildButtons) expect(screen.queryByRole('button', { name })).toBeNull();
      view.unmount();
    }
  });

  it('keeps the page actions menu on a page', async () => {
    await mount('#/debates/wiki/cli-llm-agent');
    openActions();
    expect(screen.getByRole('button', { name: ko.wiki.page.rewriteAria('cli-llm-agent') })).toBeTruthy();
  });

  it('lists the build jobs of the project, newest first, and the pages that need a check', async () => {
    const older = { ...wikiJob, id: 'dbt_old', task: 'code wiki build: all pages', status: 'completed' as const, started_at: '2026-10-11T09:00:00Z', finished_at: '2026-10-11T09:20:00Z', exit_code: 0 };
    const other = { ...wikiJob, id: 'dbt_other', task: 'code wiki build: other project', project: 'elsewhere', status: 'failed' as const };
    const debate = { ...doneJob, id: 'dbt_debate' };
    mocked.fetchDebates.mockResolvedValue([older, debate, { ...wikiJob, status: 'failed' as const, finished_at: '2026-10-12T09:05:00Z', exit_code: 1 }, other]);
    await mount('#/debates/wiki?view=history');
    const table = screen.getByRole('table', { name: ko.wiki.history.tableLabel });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell')[0].textContent)).toEqual(['code wiki build: stale pages', 'code wiki build: all pages']);
    expect(within(rows[0]).getByText(ko.debate.jobStatus.failed)).toBeTruthy();
    expect(within(rows[1]).getByText(ko.debate.jobStatus.completed)).toBeTruthy();
    expect(within(rows[0]).getByRole('link', { name: ko.wiki.history.followAria(wikiJob.id) }).getAttribute('href')).toBe(`#/debates/${wikiJob.id}`);
    // The two pages that are not fresh.
    const attention = screen.getByText(ko.wiki.history.pagesTitle).parentElement as HTMLElement;
    expect(within(attention).getAllByRole('link').map((a) => a.getAttribute('href'))).toEqual(['#/debates/wiki/cli-llm-agent', '#/debates/wiki/scripts']);
  });

  it('says so when the project has no build yet', async () => {
    await mount('#/debates/wiki?view=history');
    expect(screen.getByText(ko.wiki.history.empty)).toBeTruthy();
  });

  it('keeps the banner of a build started from a page', async () => {
    await mount('#/debates/wiki/cli-llm-agent');
    openActions();
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.page.rewriteAria('cli-llm-agent') }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.wiki.page.rewriteLabel }));
    await settle();
    expect(mocked.startWikiBuild).toHaveBeenCalledWith('server-guardian', { pages: ['cli-llm-agent'], force: true });
    expect(screen.getByRole('link', { name: ko.wiki.tab.followInMonitor })).toBeTruthy();
  });

  it('hides the mode control on a narrow screen while a file is open, and goes back to the file list', async () => {
    mockNarrow(true);
    mocked.fetchCodeFiles.mockReset();
    mocked.fetchCodeFiles.mockResolvedValue({ project: 'server-guardian', sha: 'abcdef1234567', files: [], truncated: false });
    mocked.fetchCodeFile.mockReset();
    mocked.fetchCodeFile.mockRejectedValue(new Error('no such file'));
    await mount('#/debates/wiki?view=files');
    expect(screen.getByRole('group', { name: ko.wiki.modes.aria })).toBeTruthy();
    window.location.hash = '#/debates/wiki?file=apps%2Fagent%2Fmain.go';
    await settle(300);
    expect(screen.queryByRole('group', { name: ko.wiki.modes.aria })).toBeNull();
    expect(screen.getByRole('link', { name: ko.wiki.files.backToFiles }).getAttribute('href')).toBe('#/debates/wiki?view=files');
  });
});

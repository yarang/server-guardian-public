import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import { en } from '../src/i18n/en';
import type { ProjectEntry, ReviewFinding, ReviewResult, ReviewView } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { debateTaskFrom, parseReviewRoute, reviewRoles, validRange } from '../src/lib/review';
import { DebatesPage } from '../src/pages/DebatesPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { getDebatePrefill } from '../src/state/debatePrefill';

const mocked = vi.mocked(api);
const R = ko.review;
const D = ko.debate;
const ID = 'review_20261123_101112_ab12cd';

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

const finding = (over: Partial<ReviewFinding> = {}): ReviewFinding => ({
  path: 'cli/tool.py', line: 11, quote: 'return eval(x)', claim: 'risky evaluates its argument.',
  scenario: 'user text runs as code.', severity: 'high', verified: true, reason: 'ok', ...over,
});

const result = (over: Partial<ReviewResult> = {}): ReviewResult => ({
  id: ID, label: 'REVIEW', note: 'advisory, no Judge', state: 'done', project: 'server-guardian', role: 'critic_redteam',
  provider: 'claude-cli', change: { range: 'main~1..main' }, base: 'a'.repeat(40), head: 'b'.repeat(40),
  files: ['cli/tool.py'], added: 5, removed: 1,
  findings: [finding(), finding({ line: 7, quote: 'return 3', claim: 'open returns 3.', severity: 'low' })],
  notes: [finding({ path: 'conf/app.yaml', line: 2, claim: 'fixed port.', severity: 'medium', reason: 'not on a changed line' })],
  summary: '2 findings (1 high, 0 medium, 1 low), 1 notes', degraded: false, failures: [],
  created: '2026-11-23T10:11:12Z', elapsed_s: 42, ...over,
});

const view = (over: Partial<ReviewView> = {}): ReviewView => ({
  id: ID, status: 'completed', label: 'REVIEW', note: 'advisory, no Judge', result: result(), ...over,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-11-23T12:00:00Z'));
  window.localStorage.setItem('guardian.project', 'server-guardian');
  mocked.fetchProjects.mockResolvedValue({
    projects: [project('server-guardian'), project('fleet', 'ops'), project('arch-docs', 'design')], custom_dir: '/x',
  });
  mocked.fetchPersonas.mockResolvedValue({ personas: [
    { id: 'critic_redteam', name: 'Red Team', role: 'Critic Agent', domain: 'security', system_prompt: 'x' },
    { id: 'ops_judge', name: 'Ops Judge', role: 'Judge', domain: 'ops', system_prompt: 'x' },
  ] });
  mocked.fetchTeams.mockResolvedValue({ teams: [], catalog: { providers: [], installed: ['claude', 'codex'], tools: [], max_critics: 4, max_rounds: 10 } });
  mocked.fetchReviewList.mockResolvedValue([]);
  mocked.fetchReview.mockResolvedValue(view());
  mocked.startReview.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

const change = (el: Element, value: string) => fireEvent.change(el, { target: { value } });

describe('review helpers', () => {
  it('checks ranges, ids and reviewer roles', () => {
    expect(validRange('main~1..main')).toBe(true);
    expect(validRange('origin/main..feature/x')).toBe(true);
    for (const bad of ['main', 'a...b', '-x..b', '..b', 'a b..c']) expect(validRange(bad)).toBe(false);
    expect(parseReviewRoute([ID])).toEqual({ id: ID });
    expect(parseReviewRoute(['../x'])).toEqual({});
    expect(reviewRoles([
      { id: 'critic_redteam', name: 'a', role: 'Critic', domain: 'x', system_prompt: 'x' },
      { id: 'ops_judge', name: 'b', role: 'Judge', domain: 'x', system_prompt: 'x' },
    ]).map((p) => p.id)).toEqual(['critic_redteam']);
  });

  it('builds a debate task that fits the form limit and keeps the change whole', () => {
    const task = debateTaskFrom(result(), 'Review:', 'Findings:', 4000);
    expect(task.startsWith('Review: main~1..main (cli/tool.py)')).toBe(true);
    expect(task).toContain('- [high] cli/tool.py:11 risky evaluates its argument.');
    const short = debateTaskFrom(result(), 'Review:', 'Findings:', 60);
    expect(short.length).toBeLessThanOrEqual(60);
  });

  it('has the same keys in both catalogs', () => {
    expect(Object.keys(en.review).sort()).toEqual(Object.keys(ko.review).sort());
  });
});

describe('Quick review tab', () => {
  it('comes first for a code project and is not offered for ops or design projects', async () => {
    await mount('#/debates');
    const tabs = within(screen.getByRole('group', { name: D.page.sections })).getAllByRole('button').map((b) => b.textContent);
    expect(tabs[0]).toBe(D.page.tabReview);
    expect(tabs[tabs.length - 1]).toBe(D.page.tabResidents);
    document.body.innerHTML = '';
    for (const id of ['fleet', 'arch-docs']) {
      window.localStorage.setItem('guardian.project', id);
      await mount('#/debates');
      expect(screen.queryByRole('button', { name: D.page.tabReview })).toBeNull();
      document.body.innerHTML = '';
    }
  });

  it('starts a range review with the chosen reviewer and provider, and follows it', async () => {
    mocked.startReview.mockResolvedValue({ id: ID, status: 'running', label: 'REVIEW', note: 'advisory, no Judge' });
    await mount('#/debates/review');
    change(screen.getByLabelText(R.range), 'main');
    fireEvent.click(screen.getByRole('button', { name: R.start }));
    await settle();
    expect(screen.getByText(R.badRange)).toBeTruthy();
    expect(mocked.startReview).not.toHaveBeenCalled();
    change(screen.getByLabelText(R.range), ' main~1..main ');
    change(screen.getByLabelText(R.role), 'critic_redteam');
    change(screen.getByLabelText(R.provider), 'codex');
    expect(within(screen.getByLabelText(R.role)).queryByRole('option', { name: /ops_judge/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: R.start }));
    await settle();
    expect(mocked.startReview).toHaveBeenCalledWith({ project: 'server-guardian', range: 'main~1..main', role: 'critic_redteam', provider: 'codex' });
    expect(window.location.hash).toBe(`#/debates/review/${ID}`);
  });

  it('sends a pasted patch with its base, ending in a newline', async () => {
    mocked.startReview.mockResolvedValue({ id: ID, status: 'running', label: 'REVIEW', note: 'advisory, no Judge' });
    await mount('#/debates/review');
    fireEvent.click(screen.getByRole('button', { name: R.modePatch }));
    fireEvent.click(screen.getByRole('button', { name: R.start }));
    await settle();
    expect(screen.getByText(R.emptyPatch)).toBeTruthy();
    change(screen.getByLabelText(R.patch), '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b');
    change(screen.getByLabelText(R.patchBase), 'main');
    fireEvent.click(screen.getByRole('button', { name: R.start }));
    await settle();
    expect(mocked.startReview).toHaveBeenCalledWith({ project: 'server-guardian', patch: '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n', patch_base: 'main' });
  });

  it('shows a hub refusal in the form', async () => {
    mocked.startReview.mockRejectedValue(new Error('range: the change is empty'));
    await mount('#/debates/review');
    change(screen.getByLabelText(R.range), 'a..b');
    fireEvent.click(screen.getByRole('button', { name: R.start }));
    await settle();
    expect(screen.getByText(/the change is empty/)).toBeTruthy();
  });

  it('lists the recent reviews of the project with their counts', async () => {
    mocked.fetchReviewList.mockResolvedValue([{ id: ID, status: 'completed', label: 'REVIEW', note: 'advisory, no Judge', state: 'done',
      project: 'server-guardian', range: 'main~1..main', counts: { high: 1, medium: 0, low: 1, notes: 1 }, created: '2026-11-23T10:00:00Z' }]);
    await mount('#/debates/review');
    expect(mocked.fetchReviewList).toHaveBeenCalledWith('server-guardian');
    const row = screen.getByRole('link', { name: R.openRun('main~1..main') });
    expect(row.getAttribute('href')).toBe(`#/debates/review/${ID}`);
    expect(within(row).getByText(R.counts(1, 0, 1))).toBeTruthy();
  });
});

describe('Quick review result', () => {
  it('shows the label with no-Judge line, the findings by severity and the notes folded', async () => {
    await mount(`#/debates/review/${ID}`);
    expect(screen.getByText('REVIEW')).toBeTruthy();
    expect(screen.getByText(R.noJudge)).toBeTruthy();
    expect(screen.queryByText(/APPROVED|PASSED/)).toBeNull();
    expect(screen.getByText(R.change('main~1..main'))).toBeTruthy();
    const high = screen.getByRole('region', { name: R.severityGroup(R.severity.high, 1) });
    expect(within(high).getByText('cli/tool.py:11')).toBeTruthy();
    expect(within(high).getByText('return eval(x)')).toBeTruthy();
    expect(screen.getByRole('region', { name: R.severityGroup(R.severity.low, 1) })).toBeTruthy();
    const notes = screen.getByText(/fixed port\./).closest('details') as HTMLDetailsElement;
    expect(notes.open).toBe(false);
    fireEvent.click(screen.getByText(R.notesTitle(1)));
    expect(notes.open).toBe(true);
  });

  it('hands the change and its findings to a debate', async () => {
    await mount(`#/debates/review/${ID}`);
    fireEvent.click(screen.getByRole('button', { name: R.runAsDebate }));
    const prefill = getDebatePrefill();
    expect(prefill?.task.startsWith(`${R.debateIntro} main~1..main`)).toBe(true);
    expect(window.location.hash).toBe('#/debates');
  });

  it('says so for a fallback answer and an unreadable findings block, keeping the text', async () => {
    mocked.fetchReview.mockResolvedValue(view({ result: result({ state: 'bad_format', findings: [], notes: [], text: 'No block here.' }) }));
    await mount(`#/debates/review/${ID}`);
    expect(screen.getByText(R.badFormat)).toBeTruthy();
    expect(screen.getByText('No block here.')).toBeTruthy();
    document.body.innerHTML = '';
    mocked.fetchReview.mockResolvedValue(view({ result: result({ state: 'degraded', degraded: true, findings: [], notes: [] }) }));
    await mount(`#/debates/review/${ID}`);
    expect(screen.getByText(R.degraded)).toBeTruthy();
  });
});

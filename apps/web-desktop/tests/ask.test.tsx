import { act, fireEvent, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { PersonaList, ProjectEntry, SoloListItem, SoloResult, SoloView } from '../src/types';

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
const A = ko.ask;
const D = ko.debate;

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

const personas: PersonaList = {
  personas: [
    { id: 'sec-expert', name: 'Security Expert', role: 'Domain Expert Agent', domain: 'Linux hardening', system_prompt: 'You harden hosts.' },
    { id: 'ops_judge', name: 'Ops Judge', role: 'JEV-MCP Decision Agent', domain: 'Operations', system_prompt: 'You decide.' },
    { id: 'jev_mcp_decision', name: 'JEV Decision', role: 'JEV-MCP Decision Agent', domain: 'Decisions', system_prompt: 'You decide.' },
  ],
};

const ID = 'solo_20261030_101112_ab12cd';

const result = (over: Partial<SoloResult> = {}): SoloResult => ({
  id: ID,
  label: 'SOLO',
  note: 'unverified, no Judge',
  project: 'server-guardian',
  role: 'sec-expert',
  provider: 'claude-cli',
  question: 'Where is the retry logic?',
  answer: '## Retry\n\nIn `runner.go:40`.\n\n```findings\n[{"id": 1}]\n```',
  degraded: false,
  citations: { verified: ['cli/runner.go'], unshown: [], missing: [] },
  summary: '1 verified, 0 unshown, 0 missing',
  steps: 2,
  failures: [],
  created: '2026-10-30T10:11:12Z',
  elapsed_s: 12.5,
  ...over,
});

const view = (over: Partial<SoloView> = {}): SoloView => ({
  id: ID,
  status: 'completed',
  label: 'SOLO',
  note: 'unverified, no Judge',
  result: result(),
  ...over,
});

const listItem = (over: Partial<SoloListItem> = {}): SoloListItem => ({
  id: ID,
  status: 'completed',
  label: 'SOLO',
  note: 'unverified, no Judge',
  ...over,
});

const writeText = vi.fn(async () => undefined);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-30T12:00:00Z'));
  window.localStorage.setItem('guardian.project', 'server-guardian');
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  writeText.mockClear();
  mocked.fetchProjects.mockResolvedValue({
    projects: [project('server-guardian'), project('fleet', 'ops'), project('arch-docs', 'design')],
    custom_dir: '/x',
  });
  mocked.fetchPersonas.mockResolvedValue(personas);
  // "Run as debate" lands on the Debates view, whose team picker loads this.
  mocked.fetchTeams.mockResolvedValue({ teams: [], catalog: { providers: [], installed: [], tools: [], max_critics: 4, max_rounds: 10 } });
  mocked.fetchSoloList.mockResolvedValue([]);
  mocked.fetchSolo.mockResolvedValue(view());
});

afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

const change = (el: Element, value: string) => fireEvent.change(el, { target: { value } });

describe('Quick ask tab', () => {
  it.each([
    ['code', 'server-guardian'],
    ['ops', 'fleet'],
    ['design', 'arch-docs'],
  ])('is offered for a %s project', async (_kind, id) => {
    window.localStorage.setItem('guardian.project', id);
    await mount('#/debates');
    expect(screen.getByRole('button', { name: D.page.tabAsk })).toBeTruthy();
  });

  it('posts the question with the chosen role, provider and reference code, then follows the run', async () => {
    mocked.startSolo.mockResolvedValue({ id: 'solo_20261030_120000_ffffff', status: 'running', label: 'SOLO', note: 'unverified, no Judge' });
    mocked.fetchSolo.mockResolvedValue(view({ id: 'solo_20261030_120000_ffffff', status: 'running', result: undefined }));
    await mount('#/debates/ask');
    change(screen.getByLabelText(A.question), 'Where is the retry logic?');
    change(screen.getByLabelText(A.role), 'sec-expert');
    change(screen.getByLabelText(A.provider), 'codex');
    fireEvent.click(screen.getByText(A.advanced));
    change(screen.getByLabelText(D.start.refCode), 'apps/agent/*.go\n\n cli/**/*.py\n');
    fireEvent.click(screen.getByRole('button', { name: A.ask }));
    await settle();
    expect(mocked.startSolo).toHaveBeenCalledWith({
      question: 'Where is the retry logic?',
      project: 'server-guardian',
      role: 'sec-expert',
      provider: 'codex',
      context_paths: ['apps/agent/*.go', 'cli/**/*.py'],
    });
    expect(window.location.hash).toBe('#/debates/ask/solo_20261030_120000_ffffff');
  });

  it('asks a general question without project or reference code, keeping the chosen provider', async () => {
    mocked.startSolo.mockResolvedValue({ id: 'solo_20261030_120000_ffffff', status: 'running', label: 'SOLO', note: 'unverified, no Judge' });
    await mount('#/debates/ask');
    change(screen.getByLabelText(A.provider), 'codex');
    fireEvent.click(screen.getByRole('checkbox', { name: A.general }));
    // The provider list and the chosen provider stay; only the reference code leaves.
    expect((screen.getByLabelText(A.provider) as HTMLSelectElement).value).toBe('codex');
    expect(Array.from((screen.getByLabelText(A.provider) as HTMLSelectElement).options).map((o) => o.value)).toEqual(['', 'claude', 'codex', 'agy']);
    expect(screen.queryByText(A.advanced)).toBeNull();
    expect(screen.queryByLabelText(D.start.refCode)).toBeNull();
    expect(screen.getByText(A.generalHint)).toBeTruthy();
    change(screen.getByLabelText(A.question), 'What is DNS?');
    fireEvent.click(screen.getByRole('button', { name: A.ask }));
    await settle();
    expect(mocked.startSolo).toHaveBeenCalledWith({ question: 'What is DNS?', general: true, provider: 'codex' });
    expect(window.location.hash).toBe('#/debates/ask/solo_20261030_120000_ffffff');
  });

  it('offers the providers the hub has installed, and drops zai when the question is general', async () => {
    mocked.fetchTeams.mockResolvedValue({
      teams: [],
      catalog: { providers: [], installed: ['claude', 'zai', 'opencode'], tools: [], max_critics: 4, max_rounds: 10 },
    });
    mocked.startSolo.mockResolvedValue({ id: 'solo_20261030_120000_ffffff', status: 'running', label: 'SOLO', note: 'unverified, no Judge' });
    await mount('#/debates/ask');
    const select = () => screen.getByLabelText(A.provider) as HTMLSelectElement;
    const values = () => Array.from(select().options).map((o) => o.value);
    expect(values()).toEqual(['', 'claude', 'zai', 'opencode']);
    change(select(), 'zai');
    fireEvent.click(screen.getByRole('checkbox', { name: A.general }));
    // zai may not answer a general question: it leaves the list and the selection resets to the default.
    expect(values()).toEqual(['', 'claude', 'opencode']);
    expect(select().value).toBe('');
    change(select(), 'opencode');
    change(screen.getByLabelText(A.question), 'What is DNS?');
    fireEvent.click(screen.getByRole('button', { name: A.ask }));
    await settle();
    expect(mocked.startSolo).toHaveBeenCalledWith({ question: 'What is DNS?', general: true, provider: 'opencode' });
  });

  it('hides the reference code for an ops project and sends none', async () => {
    window.localStorage.setItem('guardian.project', 'fleet');
    mocked.startSolo.mockResolvedValue({ id: 'solo_20261030_120000_ffffff', status: 'running', label: 'SOLO', note: 'unverified, no Judge' });
    await mount('#/debates/ask');
    expect(screen.queryByLabelText(D.start.refCode)).toBeNull();
    change(screen.getByLabelText(A.question), 'Which nodes have full disks?');
    fireEvent.click(screen.getByRole('button', { name: A.ask }));
    await settle();
    expect(mocked.startSolo).toHaveBeenCalledWith({ question: 'Which nodes have full disks?', project: 'fleet' });
  });

  it('does not offer the personas a solo run refuses (judges)', async () => {
    await mount('#/debates/ask');
    const role = screen.getByLabelText(A.role) as HTMLSelectElement;
    const ids = Array.from(role.options).map((o) => o.value);
    expect(ids).toEqual(['', 'sec-expert']);
  });

  it('shows a start refusal from the hub in the form', async () => {
    mocked.startSolo.mockRejectedValue(new api.HubError('too many running solo runs (max 2)', 429));
    await mount('#/debates/ask');
    change(screen.getByLabelText(A.question), 'Where is the retry logic?');
    fireEvent.click(screen.getByRole('button', { name: A.ask }));
    await settle();
    expect(screen.getByRole('alert').textContent).toBe('too many running solo runs (max 2)');
  });

  it('disables asking while the question is empty', async () => {
    await mount('#/debates/ask');
    expect((screen.getByRole('button', { name: A.ask }) as HTMLButtonElement).disabled).toBe(true);
    change(screen.getByLabelText(A.question), '  ');
    expect((screen.getByRole('button', { name: A.ask }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('Quick ask detail', () => {
  it('polls a running run, then shows the answer with the SOLO label and stops polling', async () => {
    let phase: 'running' | 'done' = 'running';
    mocked.fetchSolo.mockImplementation(async () => (phase === 'running' ? view({ status: 'running', result: undefined }) : view()));
    await mount(`#/debates/ask/${ID}`);
    expect(screen.getByText('SOLO')).toBeTruthy();
    expect(screen.getByText(A.note)).toBeTruthy();
    expect(screen.getByText(A.waiting)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Retry' })).toBeNull();

    phase = 'done';
    await settle(2000);
    await settle(2000);
    expect(screen.getByRole('heading', { name: 'Retry' })).toBeTruthy();
    expect(screen.getByText(A.citations(result().summary))).toBeTruthy();
    expect(screen.getByText(A.elapsed(12.5))).toBeTruthy();
    expect(screen.getByText(A.providerLabel('claude-cli'))).toBeTruthy();
    // The findings tail is machine output: it is not rendered.
    expect(screen.queryByText('"id": 1')).toBeNull();
    // Finished: no further polling of the run.
    const calls = mocked.fetchSolo.mock.calls.length;
    await settle(20_000);
    expect(mocked.fetchSolo.mock.calls.length).toBe(calls);
  });

  it('shows a failed run with its error and no answer', async () => {
    mocked.fetchSolo.mockResolvedValue(view({ status: 'failed', error: 'the run ended without a result', result: undefined }));
    await mount(`#/debates/ask/${ID}`);
    expect(screen.getByRole('alert').textContent).toContain('the run ended without a result');
    expect(screen.queryByRole('heading', { name: 'Retry' })).toBeNull();
    expect(screen.queryByRole('button', { name: A.copy })).toBeNull();
  });

  it('warns when every provider failed (degraded)', async () => {
    mocked.fetchSolo.mockResolvedValue(view({ result: result({ degraded: true, provider: 'built-in-expert' }) }));
    await mount(`#/debates/ask/${ID}`);
    expect(screen.getByRole('status').textContent).toBe(A.degraded);
  });

  it('copies the answer without the findings block', async () => {
    mocked.fetchSolo.mockResolvedValue(view());
    await mount(`#/debates/ask/${ID}`);
    await settle(100);
    fireEvent.click(screen.getByRole('button', { name: A.copy }));
    await settle();
    expect(writeText).toHaveBeenCalledWith('## Retry\n\nIn `runner.go:40`.');
  });

  it('hands the question and the stripped answer to the New debate form', async () => {
    mocked.fetchSolo.mockResolvedValue(view());
    await mount(`#/debates/ask/${ID}`);
    await settle(100);
    fireEvent.click(screen.getByRole('button', { name: A.runAsDebate }));
    await settle();
    expect(window.location.hash).toBe('#/debates');
    const task = screen.getByLabelText(D.start.task) as HTMLTextAreaElement;
    expect(task.value).toBe(`Where is the retry logic?\n\n${A.earlier}\n## Retry\n\nIn \`runner.go:40\`.`);
  });

  it('shows a general answer with its own note, no sources and no debate hand-off', async () => {
    mocked.fetchSolo.mockResolvedValue(view({
      result: result({
        mode: 'general',
        project: '',
        note: 'unverified, no Judge, no sources',
        summary: 'no sources',
        citations: { verified: [], unshown: [], missing: [] },
      }),
    }));
    await mount(`#/debates/ask/${ID}`);
    expect(screen.getByRole('heading', { name: 'Retry' })).toBeTruthy();
    expect(screen.getByText(A.generalLabel)).toBeTruthy();
    expect(screen.getByText('unverified, no Judge, no sources')).toBeTruthy();
    expect(screen.getByText(A.noSources)).toBeTruthy();
    expect(screen.queryByText(A.citations('no sources'))).toBeNull();
    // A debate needs a project: a general question cannot be handed to one.
    expect(screen.queryByRole('button', { name: A.runAsDebate })).toBeNull();
    expect(screen.getByRole('button', { name: A.copy })).toBeTruthy();
  });

  it('goes back to the form through the list link', async () => {
    mocked.fetchSolo.mockResolvedValue(view());
    await mount(`#/debates/ask/${ID}`);
    expect(screen.getByRole('link', { name: A.back }).getAttribute('href')).toBe('#/debates/ask');
  });
});

describe('Quick ask recent list', () => {
  it('lists recent solo runs and opens one', async () => {
    mocked.fetchSoloList.mockResolvedValue([
      listItem({ question: 'Where is the retry logic?\nSecond line', summary: '1 verified, 0 unshown, 0 missing', role: 'sec-expert' }),
      listItem({ id: 'solo_20261030_090000_000000', status: 'failed', question: 'Disks?' }),
    ]);
    await mount('#/debates/ask');
    const row = screen.getByRole('link', { name: A.openRun('Where is the retry logic?') });
    expect(row.getAttribute('href')).toBe(`#/debates/ask/${ID}`);
    // Only the first line of a question is the row's title.
    expect(screen.queryByText('Second line')).toBeNull();
    expect(screen.getByText(ko.agents.jobStatus.failed)).toBeTruthy();
    follow(row);
    await settle();
    expect(mocked.fetchSolo).toHaveBeenCalledWith(ID);
  });

  it('marks a general question with its own label', async () => {
    mocked.fetchSoloList.mockResolvedValue([listItem({ mode: 'general', question: 'What is DNS?', summary: 'no sources' })]);
    await mount('#/debates/ask');
    expect(screen.getByRole('link', { name: A.openRun('What is DNS?') })).toBeTruthy();
    expect(screen.getByText(A.generalLabel)).toBeTruthy();
  });

  it('says so when there are none', async () => {
    await mount('#/debates/ask');
    expect(screen.getByText(A.empty)).toBeTruthy();
  });
});

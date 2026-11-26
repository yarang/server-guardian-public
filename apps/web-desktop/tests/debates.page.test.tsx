import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { GuardianRoot, getUiMessages } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { action, doneJob, doneResult, runningJob, teamList } from './debateFixtures';
import type { ActionItem, DebateJobDetail } from '../src/types';
import { follow } from './nav';
import { ko } from '../src/i18n/ko';

const D = ko.debate;

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { getDebatePrefill, setDebatePrefill } from '../src/state/debatePrefill';

const mocked = vi.mocked(api);

/** Flush pending promises and timers (the tests run on fake timers so polling can be stepped). */
const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

/** The DebatesPage the way App renders it: `#/debates/:jobId` becomes the jobId prop. */
function Routed() {
  return <DebatesPage jobId={useRoute().params[0]} />;
}

async function mount() {
  const view = render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle();
  // The form is collapsed behind "New debate" (a hand-off from another page expands it by itself).
  if (!screen.queryByRole('button', { name: D.start.start })) fireEvent.click(screen.getByRole('button', { name: D.page.newDebate }));
  return view;
}

const change = (el: Element, value: string) => fireEvent.change(el, { target: { value } });

beforeEach(() => {
  // Drop queued once-values and calls left by a previous test.
  for (const fn of [
    mocked.fetchTeams, mocked.startDebate, mocked.fetchDebates, mocked.fetchDebate, mocked.cancelDebate, mocked.listActions,
    mocked.getAction, mocked.jevRecheck, mocked.closeThread, mocked.publishThread, mocked.getPublishPlan, mocked.fetchPublications,
  ]) {
    fn.mockReset();
  }
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-04T10:00:05Z'));
  mocked.fetchTeams.mockResolvedValue(teamList);
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.listActions.mockResolvedValue([]);
  mocked.fetchPublications.mockResolvedValue([]);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Debate console', () => {
  const alertText = () => within(screen.getByRole('region', { name: D.page.newDebate })).getByRole('alert').textContent;
  const taskBox = () => screen.getByLabelText(D.start.task) as HTMLTextAreaElement;
  const start = () => fireEvent.click(screen.getByRole('button', { name: D.start.start }));

  it('validates the form before calling the hub', async () => {
    await mount();
    start();
    expect(alertText()).toBe(D.start.enterTask);

    change(taskBox(), 'do it');
    change(screen.getByLabelText(D.start.continueThread), 'nope');
    start();
    expect(alertText()).toBe(D.start.badThread);

    change(screen.getByLabelText(D.start.continueThread), '');
    change(screen.getByLabelText(D.start.rounds), '9');
    start();
    expect(alertText()).toBe(D.start.badRounds(6));

    change(screen.getByLabelText(D.start.rounds), '');
    change(screen.getByLabelText(new RegExp('^' + D.result.refCode)), '-x');
    start();
    expect(alertText()).toContain(D.lib.globDash('-x'));
    expect(mocked.startDebate).not.toHaveBeenCalled();
  });

  it('names the lineup that runs without a chosen flow and sends no team (the team picker is gone)', async () => {
    mocked.startDebate.mockResolvedValue(runningJob);
    await mount();
    expect(screen.getByText(D.start.projectFlowNone)).toBeTruthy();
    change(taskBox(), 'Harden sshd');
    start();
    await settle();
    expect(mocked.startDebate).toHaveBeenCalledWith({ task: 'Harden sshd', project: 'server-guardian' });
  });

  it('starts a debate, follows it in the live monitor and swaps in the result when it finishes', async () => {
    mocked.startDebate.mockResolvedValue(runningJob);
    let current: DebateJobDetail = { ...runningJob, log: 'round 1 started', result: null };
    mocked.fetchDebate.mockImplementation(async () => current);
    await mount();

    change(taskBox(), 'Harden sshd');
    start();
    await settle();
    // Started: the URL names the job, the form collapses and the live monitor (not the result) is shown.
    expect(window.location.hash).toBe('#/debates/job1');
    expect(screen.getAllByText(ko.agents.jobStatus.running).length).toBeGreaterThan(0);
    expect(screen.getByTestId('run-elapsed')).toBeTruthy();
    expect(screen.queryByRole('region', { name: D.result.title })).toBeNull();
    expect(screen.queryByRole('link', { name: /Monitor debate/ })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Open debate result' })).toBeNull();

    current = doneJob;
    await settle(3000);
    await settle(3000);
    // Finished: the same selection now shows the result with its Publish / approvals sections.
    expect(within(screen.getByRole('region', { name: D.result.title })).getAllByText(D.decisionLabel.DISPUTED).length).toBeGreaterThan(0);
    expect(screen.getByText(/Disable password login after verifying key access/)).toBeTruthy();
    expect(screen.queryByTestId('run-elapsed')).toBeNull();

    // Finished: no further polling of the job.
    const calls = mocked.fetchDebate.mock.calls.length;
    await settle(30_000);
    expect(mocked.fetchDebate.mock.calls.length).toBe(calls);
  });

  it('stops polling on unmount', async () => {
    mocked.startDebate.mockResolvedValue(runningJob);
    mocked.fetchDebate.mockResolvedValue({ ...runningJob, log: 'x', result: null });
    const view = await mount();
    change(taskBox(), 'Harden sshd');
    start();
    await settle();
    await settle(3000);
    view.unmount();
    const calls = mocked.fetchDebate.mock.calls.length;
    await settle(30_000);
    expect(mocked.fetchDebate.mock.calls.length).toBe(calls);
  });

  it('cancels a running debate from the live monitor', async () => {
    mocked.startDebate.mockResolvedValue(runningJob);
    let current: DebateJobDetail = { ...runningJob, log: '', result: null };
    mocked.fetchDebate.mockImplementation(async () => current);
    mocked.cancelDebate.mockImplementation(async () => {
      current = { ...runningJob, status: 'cancelled', log: 'stopped', result: null };
    });
    await mount();
    change(taskBox(), 'Harden sshd');
    start();
    await settle();
    fireEvent.click(screen.getByRole('button', { name: ko.shell.cancelDebate.cancel }));
    // Declining keeps the debate running.
    fireEvent.click(within(screen.getByRole('dialog', { name: ko.shell.cancelDebate.titleDebate })).getByRole('button', { name: ko.shell.cancelDebate.keep }));
    expect(mocked.cancelDebate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: ko.shell.cancelDebate.cancel }));
    fireEvent.click(within(screen.getByRole('dialog', { name: ko.shell.cancelDebate.titleDebate })).getByRole('button', { name: ko.shell.cancelDebate.titleDebate }));
    await settle();
    expect(mocked.cancelDebate).toHaveBeenCalledWith('job1');
    expect(screen.getAllByText(ko.agents.jobStatus.cancelled).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: ko.shell.cancelDebate.cancel })).toBeNull();
  });

  it('shows a start failure from the hub', async () => {
    mocked.startDebate.mockRejectedValue(new api.HubError('too many running debates', 429));
    await mount();
    change(taskBox(), 'Harden sshd');
    start();
    await settle();
    expect(alertText()).toBe('too many running debates');
  });

  it('consumes the prefill exactly once', async () => {
    setDebatePrefill('Review the fleet', 'assistant');
    const first = await mount();
    expect(taskBox().value).toBe('Review the fleet');
    expect(getDebatePrefill()).toBeNull();

    change(taskBox(), 'my own text');
    first.unmount();
    await mount();
    expect(taskBox().value).toBe('');
  });

  it('lists recent debates with a code badge and opens one on click', async () => {
    mocked.fetchDebates.mockResolvedValue([doneJob, { ...runningJob, id: 'job0', task: 'Older task', status: 'failed' }]);
    mocked.fetchDebate.mockResolvedValue(doneJob);
    await mount();
    expect(screen.getByText(D.page.listTitleCount(2))).toBeTruthy();
    expect(screen.getAllByText(ko.agents.list.code)).toHaveLength(1);
    const row = screen.getByRole('link', { name: ko.agents.list.openDebate('job1') });
    expect(row.getAttribute('href')).toBe('#/debates/job1');
    follow(row);
    await settle();
    expect(window.location.hash).toBe('#/debates/job1');
    expect(mocked.fetchDebate).toHaveBeenCalledWith('job1');
    expect(within(screen.getByRole('region', { name: D.result.title })).getAllByText(D.decisionLabel.DISPUTED).length).toBeGreaterThan(0);
  });

  it('shows the retrieval cost on recent debates that report it', async () => {
    mocked.fetchDebates.mockResolvedValue([
      { ...doneJob, retrieval: { results: 176, bytes: 2 * 1024 * 1024 } },
      { ...runningJob, id: 'job0', task: 'Older task', status: 'failed' },
    ]);
    await mount();
    expect(screen.getAllByText(new RegExp(ko.common.retrieval(0, 'SZ').replace('0', '\\d+').replace('SZ', '.*')))).toHaveLength(1);
    expect(screen.getByText(ko.common.retrieval(176, '2.0 MB'))).toBeTruthy();
  });

  it('has one list: rows open the debate on this page and there are no Monitor / result ping-pong links', async () => {
    mocked.fetchDebates.mockResolvedValue([doneJob]);
    mocked.fetchDebate.mockResolvedValue(doneJob);
    await mount();
    expect(screen.queryByRole('link', { name: 'Monitor debate job1' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Open debate result' })).toBeNull();
    follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
    await settle();
    expect(window.location.hash).toBe('#/debates/job1');
    expect(screen.queryByRole('link', { name: /Monitor debate/ })).toBeNull();
    expect(screen.getAllByRole('link', { name: ko.agents.list.openDebate('job1') })).toHaveLength(1);
  });
});

describe('Close thread', () => {
  const closedJob = { ...doneJob, thread_closed: true };
  const approvedJob = {
    ...doneJob,
    result: { ...doneJob.result!, final_decision: { ...doneJob.result!.final_decision!, approved: true, jev_gate: 'PASSED', actions: [] } },
  };

  async function open(job: typeof doneJob, list = [job]) {
    mocked.fetchDebates.mockResolvedValue(list);
    mocked.fetchDebate.mockResolvedValue(job);
    await mount();
    follow(screen.getByRole('link', { name: ko.agents.list.openDebate(job.id) }));
    await settle();
  }

  it('confirms, closes the thread, then reloads the job and the list', async () => {
    mocked.closeThread.mockResolvedValue({ ok: true, thread_id: 'thread_abc123', status: 'CLOSED' });
    await open(doneJob);
    fireEvent.click(screen.getByRole('button', { name: D.result.closeThread }));
    const dialog = screen.getByRole('dialog', { name: D.result.closeThread });
    expect(within(dialog).getByText(/thread_abc123/)).toBeTruthy();
    expect(mocked.closeThread).not.toHaveBeenCalled();

    mocked.fetchDebate.mockResolvedValue(closedJob);
    mocked.fetchDebates.mockResolvedValue([closedJob]);
    fireEvent.click(within(dialog).getByRole('button', { name: D.result.closeThread }));
    await settle();
    expect(mocked.closeThread).toHaveBeenCalledWith('thread_abc123');
    expect(screen.queryByRole('dialog')).toBeNull();
    const result = screen.getByRole('region', { name: D.result.title });
    expect(within(result).getByText(D.job.closed)).toBeTruthy();
    expect(within(result).queryByRole('button', { name: D.result.closeThread })).toBeNull();
    expect(within(result).getByRole('button', { name: D.result.reopen })).toBeTruthy();
    // The closed debate left the default list.
    expect(screen.getByText(D.page.closedHidden(1))).toBeTruthy();
  });

  it('keeps the dialog open with the hub error when closing fails', async () => {
    mocked.closeThread.mockRejectedValue(new api.HubError('a debate is running for this thread', 409));
    await open(doneJob);
    fireEvent.click(screen.getByRole('button', { name: D.result.closeThread }));
    fireEvent.click(within(screen.getByRole('dialog', { name: D.result.closeThread })).getByRole('button', { name: D.result.closeThread }));
    await settle();
    expect(within(screen.getByRole('dialog', { name: D.result.closeThread })).getByText(/a debate is running for this thread/)).toBeTruthy();
  });

  it('does not offer closing for an APPROVED thread', async () => {
    await open(approvedJob);
    expect(within(screen.getByRole('region', { name: D.result.title })).getAllByText(D.decisionLabel.APPROVED).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: D.result.closeThread })).toBeNull();
  });

  it('hides closed debates by default and shows them with the toggle', async () => {
    const open1 = { ...runningJob, id: 'job2', task: 'Open task', status: 'completed' as const };
    mocked.fetchDebates.mockResolvedValue([closedJob, open1]);
    await mount();
    expect(screen.getByText(D.page.listTitleCount(1))).toBeTruthy();
    expect(screen.queryByRole('link', { name: ko.agents.list.openDebate('job1') })).toBeNull();
    expect(screen.getByRole('link', { name: ko.agents.list.openDebate('job2') })).toBeTruthy();

    fireEvent.click(screen.getByRole('checkbox', { name: D.page.showClosed }));
    expect(screen.getByText(D.page.listTitleCount(2))).toBeTruthy();
    const row = screen.getByRole('link', { name: ko.agents.list.openDebate('job1') });
    expect(within(row).getByText(D.job.closed)).toBeTruthy();
  });
});

describe('Debate start form', () => {
  const advanced = () => (screen.getByText(new RegExp('^' + D.start.advanced.split(':')[0] + ':')).closest('details') as HTMLDetailsElement);

  it('puts the task first and the optional fields in a collapsed Advanced section', async () => {
    await mount();
    const form = screen.getByRole('region', { name: D.page.newDebate });
    const fields = Array.from(form.querySelectorAll('textarea, select, input')) as HTMLElement[];
    expect(fields[0]).toBe(screen.getByLabelText(D.start.task));
    expect(advanced().open).toBe(false);
    for (const label of [D.start.rounds, D.start.continueThread, D.start.refCode, 'Ref']) {
      expect(advanced().contains(screen.getByLabelText(label))).toBe(true);
    }
    expect(screen.getByText(D.start.refCodeHint)).toBeTruthy();
  });

  it('opens Advanced for a problem with one of its fields', async () => {
    await mount();
    change(screen.getByLabelText(D.start.task), 'do it');
    change(screen.getByLabelText(D.start.rounds), '9');
    fireEvent.click(screen.getByRole('button', { name: D.start.start }));
    await settle();
    expect(screen.getByText(D.start.badRounds(6))).toBeTruthy();
    expect(advanced().open).toBe(true);
  });

  it('opens Advanced when a thread is continued', async () => {
    mocked.fetchDebates.mockResolvedValue([doneJob]);
    mocked.fetchDebate.mockResolvedValue(doneJob);
    await mount();
    follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
    await settle();
    fireEvent.click(screen.getByRole('button', { name: D.result.continueThread }));
    expect(advanced().open).toBe(true);
  });
});

describe('Debate result', () => {
  async function openDone() {
    mocked.fetchDebates.mockResolvedValue([doneJob]);
    mocked.fetchDebate.mockResolvedValue(doneJob);
    const view = await mount();
    follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
    await settle();
    return view;
  }

  it('renders the status, verdict chain, decision, checks, judgment and gate reasons', async () => {
    await openDone();
    const result = screen.getByRole('region', { name: D.result.title });
    // The status chip, the jev step and the gate step all read DISPUTED.
    expect(within(result).getAllByText(D.decisionLabel.DISPUTED)).toHaveLength(3);
    expect(within(result).getByText(D.job.team('cross-review'))).toBeTruthy();
    expect(within(result).getByText(D.result.thread('thread_abc123'))).toBeTruthy();

    const chain = within(result).getByRole('list', { name: getUiMessages().verdictChain });
    expect(within(chain).getByText(D.result.llmJudge)).toBeTruthy();
    expect(within(chain).getByText('jev-mcp')).toBeTruthy();
    expect(within(chain).getByText(D.result.gate)).toBeTruthy();

    expect(within(result).getByText('Disable password login after verifying key access.')).toBeTruthy();
    expect(within(result).getByText('sshd -t passes')).toBeTruthy();
    expect(within(result).getByText('PasswordAuthentication is no')).toBeTruthy();
    expect(within(result).getByText('Judge says adopt the hardened approach.')).toBeTruthy();
    expect(screen.getByText('The rollback step is not verified')).toBeTruthy();
  });

  describe('structured decision', () => {
    const fence = (lang: string, body: string) => '```' + lang + '\n' + body + '\n```';
    const rich = {
      ...doneResult,
      final_decision: {
        ...doneResult.final_decision!,
        decision: [
          '요약 문장입니다.',
          '[R1] 보안: safe_load를 쓴다 (guard.py).',
          '[R2] 시간: 시뮬레이션 시간을 쓴다.',
          '[추가] 로그: 회전을 건다.',
          '마무리 문장입니다.',
        ].join('\n\n'),
        judgment: `판단 본문.\n\n${fence('json', '{"decision":"x","actions":[]}')}\n\n${fence('findings', '[{"id":1}]')}`,
        rationale: '1) 문제의 존재: 위험하다.\n2) 대안 검토: (a) 그대로 (b) 패치',
        constraints: ['파일은 하나만 쓴다'],
        constraint_refs: [['R1']],
        checks: ['guard.py 가 로드된다'],
        check_refs: [['R1']],
        actions: [{ id: 41, tool: 'jev_mcp:execute_code_action', status: 'PROPOSED', refs: ['R1'], file_path: 'src/guard.py' }],
      },
    };
    const liveFile = action({
      id: 41,
      tool: 'jev_mcp:execute_code_action',
      status: 'PROPOSED',
      arguments: { file_path: 'src/guard.py', content: 'print("<b>hi</b>")', run_test: false },
      reason: '가드 추가',
    });
    async function openRich(result: typeof doneResult = rich) {
      mocked.listActions.mockResolvedValue([liveFile]);
      mocked.fetchDebates.mockResolvedValue([doneJob]);
      mocked.fetchDebate.mockResolvedValue({ ...doneJob, result });
      const view = await mount();
      follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
      await settle();
      return view;
    }

    it('renders summary, requirement cards, extras and the closing callout', async () => {
      await openRich();
      const result = screen.getByRole('region', { name: D.result.title });
      expect(within(result).getByText('요약 문장입니다.')).toBeTruthy();
      const cards = within(result).getByRole('region', { name: D.result.requirementsLabel });
      expect(within(cards).getByText('R1')).toBeTruthy();
      expect(within(cards).getByText('보안')).toBeTruthy();
      expect(within(cards).getByText('시간')).toBeTruthy();
      expect(within(cards).getByText('시뮬레이션 시간을 쓴다.')).toBeTruthy();
      expect(within(result).getByText(D.result.extras)).toBeTruthy();
      expect(within(result).getByText('마무리 문장입니다.')).toBeTruthy();
    });

    it('hides the judge JSON behind a disclosure', async () => {
      const { container } = await openRich();
      const result = screen.getByRole('region', { name: D.result.title });
      expect(within(result).getByText('판단 본문.')).toBeTruthy();
      const summary = within(result).getByText(D.result.rawJson);
      const details = summary.closest('details')!;
      expect(details.open).toBe(false);
      expect(details.textContent).toContain('"decision": "x"');
      expect(details.textContent).toContain('"id": 1');
      // Nothing of the fences remains in the rendered Markdown outside the disclosure.
      const outside = Array.from(container.querySelectorAll('pre')).filter((p) => !details.contains(p));
      expect(outside.some((p) => p.textContent?.includes('"decision"'))).toBe(false);
    });

    it('lists changed files with a no-test badge, a code disclosure and unwritten mentions', async () => {
      const withMention = { ...rich, final_decision: { ...rich.final_decision, checks: ['guard.py 가 로드되고 README.md 가 갱신된다'] } };
      const { container } = await openRich(withMention);
      const list = screen.getByRole('list', { name: D.result.filesLabel });
      expect(within(list).getByText('guard.py')).toBeTruthy();
      expect(within(list).getByText('src')).toBeTruthy();
      expect(within(list).getByText(D.result.noTest)).toBeTruthy();
      expect(within(list).getByText(D.actionStatus.PROPOSED)).toBeTruthy();
      // A file starts folded: header only, no reason or content rendered.
      expect(within(list).queryByText('가드 추가')).toBeNull();
      expect(within(list).queryByText('print("<b>hi</b>")')).toBeNull();
      const card = list.querySelector('details') as HTMLDetailsElement;
      card.open = true;
      fireEvent(card, new Event('toggle'));
      await settle();
      expect(within(list).getByText('가드 추가')).toBeTruthy();
      // The content is escaped text in a code block.
      expect(within(list).getByText('print("<b>hi</b>")').closest('pre')).toBeTruthy();
      expect(container.querySelector('pre b')).toBeNull();
      // README.md is named in the checks but nothing writes it.
      const row = within(list).getByText('README.md').closest('[role="listitem"]') as HTMLElement;
      expect(within(row).getByText(D.result.noChangeAction)).toBeTruthy();
      // The approvals table keeps the only controls.
      expect(screen.getByRole('table', { name: D.result.actionsLabel })).toBeTruthy();
    });

    it('folds the file section and expands or collapses every file at once', async () => {
      mocked.listActions.mockResolvedValue([liveFile, { ...liveFile, id: 42, reason: '설정 추가', arguments: { file_path: 'configs/app.yaml', content: 'a: 1', run_test: true } }]);
      mocked.fetchDebates.mockResolvedValue([doneJob]);
      mocked.fetchDebate.mockResolvedValue({ ...doneJob, result: rich });
      await mount();
      follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
      await settle();
      const section = screen.getByLabelText(D.result.filesToggle).closest('details') as HTMLDetailsElement;
      expect(section.open).toBe(true);
      expect(screen.queryByText('설정 추가')).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: D.result.filesExpandAll }));
      await settle();
      expect(screen.getByText('설정 추가')).toBeTruthy();
      expect(screen.getByText('가드 추가')).toBeTruthy();
      fireEvent.click(screen.getByRole('button', { name: D.result.filesCollapseAll }));
      await settle();
      expect(screen.queryByText('설정 추가')).toBeNull();
    });

    it('shows rationale blocks, constraints and requirement links with a warning for an unlinked requirement', async () => {
      await openRich();
      const result = screen.getByRole('region', { name: D.result.title });
      expect(within(result).getByText(D.result.rationale)).toBeTruthy();
      expect(within(result).getByText('문제의 존재')).toBeTruthy();
      expect(within(result).getByText('그대로')).toBeTruthy();
      expect(within(result).getByText('파일은 하나만 쓴다')).toBeTruthy();
      const cards = within(result).getByRole('region', { name: D.result.requirementsLabel });
      const [r1, r2] = Array.from(cards.children) as HTMLElement[];
      expect(within(r1).getByText(D.result.linkConstraints(1))).toBeTruthy();
      expect(within(r1).getByText(D.result.linkChecks(1))).toBeTruthy();
      expect(within(r1).getByText(D.result.linkFiles(1))).toBeTruthy();
      expect(within(r1).queryByText(D.result.noLinkedChange)).toBeNull();
      expect(within(r2).getByText(D.result.noLinkedChange)).toBeTruthy();
    });

    it('keeps a legacy result without refs, rationale or constraints readable', async () => {
      const legacy = {
        ...doneResult,
        final_decision: { ...doneResult.final_decision!, decision: 'Intro. [1] Security: a. [2] Clock: b.' },
      };
      await openRich(legacy);
      const result = screen.getByRole('region', { name: D.result.title });
      const cards = within(result).getByRole('region', { name: D.result.requirementsLabel });
      expect(cards.children).toHaveLength(2);
      expect(within(result).queryByText(D.result.rationale)).toBeNull();
      expect(within(result).queryByText(D.result.constraints)).toBeNull();
      expect(within(result).queryByText(D.result.noLinkedChange)).toBeNull();
      expect(within(result).queryByText(D.result.linkFiles(0))).toBeNull();
    });
  });

  it('summarizes the reference code with files, parts, truncation and skipped reasons', async () => {
    await openDone();
    expect(screen.getByText(new RegExp(D.result.refCode + ':')).textContent).toBe(`${D.result.refCode}: 0123456${D.result.contextStats(2, '12.0 KB', 1)}`);
    expect(screen.getByText('apps/agent/main.go')).toBeTruthy();
    expect(screen.getByText(D.result.truncated)).toBeTruthy();
    expect(screen.getByText('#executeSSH, lines 110-145' + D.result.plusOutline)).toBeTruthy();
    expect(screen.getByText('secrets.enc')).toBeTruthy();
    expect(screen.getByText('secret pattern')).toBeTruthy();
  });

  it('groups the transcript by round and shows fallback lines and markup as text', async () => {
    const { container } = await openDone();
    const r1 = screen.getByText(D.result.round(1));
    const r2 = screen.getByText(D.result.round(2));
    expect(r1.compareDocumentPosition(r2) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(D.result.skippedProvider('codex: not installed'))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: D.result.expandAll }));
    await settle();
    expect(screen.getByText('Proposal text <b>bold</b>')).toBeTruthy();
    expect(container.querySelector('b')).toBeNull();
    expect(screen.getByText('agy')).toBeTruthy();
  });

  it('renders the judgment and agent answers as Markdown', async () => {
    const md = { ...doneResult, final_decision: { ...doneResult.final_decision!, judgment: '## Code check\n\n**Verified** in `main.go`\n\n```json\n{"a": 1}\n```' },
      transcript: [{ round: 1, agent: 'Sec Expert', role: 'Domain Expert Agent', content: '### Plan\n\n- step one\n- step two', provider: 'agy' }] };
    mocked.fetchDebates.mockResolvedValue([doneJob]);
    mocked.fetchDebate.mockResolvedValue({ ...doneJob, result: md });
    const { container } = await mount();
    follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
    await settle();
    expect(screen.getByRole('heading', { name: 'Code check' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: D.result.expandAll }));
    await settle();
    expect(container.querySelector('strong')?.textContent).toBe('Verified');
    expect(screen.getByText('{"a": 1}').closest('pre')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Plan' })).toBeTruthy();
    expect(screen.getByText('step two').tagName).toBe('LI');
    expect(container.textContent).not.toContain('## Code check');
  });

  it('folds each answer: closed at first, per card, per round and all at once', async () => {
    const { container } = await openDone();
    const cards = () => Array.from(container.querySelectorAll('details')).filter((d) => d.querySelector('summary[aria-label]'));
    expect(cards()).toHaveLength(3);
    expect(cards().every((d) => !d.open)).toBe(true);
    expect(screen.queryByText('Critique text')).toBeNull(); // folded answers are not rendered

    const critic = cards().find((d) => d.textContent?.includes('Ops Critic'))!;
    critic.open = true;
    fireEvent(critic, new Event('toggle'));
    await settle();
    expect(screen.getByText('Critique text')).toBeTruthy();
    expect(screen.queryByText('Final answer')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: D.result.expandRound(1) }));
    await settle();
    expect(screen.getByText('Proposal text <b>bold</b>')).toBeTruthy();
    expect(screen.getByRole('button', { name: D.result.collapseRound(1) })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: D.result.expandAll }));
    await settle();
    expect(cards().every((d) => d.open)).toBe(true);
    expect(screen.getByText('Final answer')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: D.result.collapseAll }));
    await settle();
    expect(cards().every((d) => !d.open)).toBe(true);
    expect(screen.queryByText('Critique text')).toBeNull();
  });

  it('re-runs the jev check for a disputed gate and shows the outcome', async () => {
    mocked.jevRecheck.mockResolvedValue({ ok: true, gate: 'PASSED', released: [41], review: { reasons: ['rollback verified'] } });
    await openDone();
    fireEvent.click(screen.getByRole('button', { name: D.result.recheck }));
    await settle();
    expect(mocked.jevRecheck).toHaveBeenCalledWith('thread_abc123');
    expect(screen.getByText(new RegExp('^' + D.details.released + ':')).textContent).toBe(D.details.released + ': #41');
    expect(screen.getByText('rollback verified')).toBeTruthy();
  });

  describe('publish', () => {
    const plan = {
      thread_id: 'thread_abc123',
      project_id: 'default',
      branch: 'guardian/thread_abc123',
      remote: 'origin',
      base_sha: '0123456789abcdef',
      files: ['app.py', 'lib/util.py'],
      subject: 'guardian: thread_abc123',
      retry: false,
    };
    const executed = (over: Partial<ActionItem> = {}) =>
      action({ id: 41, tool: 'jev_mcp:apply_patch', status: 'EXECUTED', ...over });
    const publishButton = () => screen.getByRole('button', { name: D.result.publish }) as HTMLButtonElement;

    async function openPublish(actions: ActionItem[] = [executed()]) {
      mocked.listActions.mockResolvedValue(actions);
      mocked.getPublishPlan.mockReset();
      mocked.getPublishPlan.mockResolvedValue(plan);
      await openDone();
    }
    async function openDialog() {
      fireEvent.click(publishButton());
      await settle();
      return screen.getByRole('dialog', { name: D.result.publishThread });
    }

    it('reads the live action statuses instead of the result snapshot', async () => {
      await openPublish([executed(), action({ id: 42, tool: 'run_tests', status: 'REJECTED' })]);
      expect(mocked.listActions).toHaveBeenCalledWith('thread_abc123');
      const table = screen.getByRole('table', { name: D.result.actionsLabel });
      expect(within(table).getByText(D.actionStatus.EXECUTED)).toBeTruthy();
      expect(within(table).getByText(D.actionStatus.REJECTED)).toBeTruthy();
      // The snapshot said HELD / FAILED.
      expect(within(table).queryByText(D.actionStatus.HELD)).toBeNull();
      expect(within(table).queryByText(D.actionStatus.FAILED)).toBeNull();
    });

    it('enables Publish when every action is settled and a file change was executed', async () => {
      await openPublish();
      expect(publishButton().disabled).toBe(false);
      expect(screen.getByText(D.result.publishNote)).toBeTruthy();
    });

    it('shows "Published as branch X (commit abc1234)" and disables Publish when a PUSHED publication exists', async () => {
      mocked.fetchPublications.mockResolvedValue([
        { id: 2, branch: 'guardian/thread_abc123', commit_sha: 'abc1234def5678', status: 'PUSHED' },
        { id: 1, branch: 'guardian/old', commit_sha: null, status: 'FAILED', error: 'push rejected' },
      ]);
      await openPublish();
      expect(mocked.fetchPublications).toHaveBeenCalledWith('thread_abc123');
      expect(screen.getAllByText(D.result.publishedAs('guardian/thread_abc123', 'abc1234')).length).toBeGreaterThan(0);
      expect(screen.getByText(D.result.alreadyPublished('guardian/thread_abc123', 'abc1234'))).toBeTruthy();
      expect(publishButton().disabled).toBe(true);
    });

    it('keeps Publish enabled when the only publication failed', async () => {
      mocked.fetchPublications.mockResolvedValue([{ id: 1, branch: 'guardian/x', commit_sha: 'abc1234', status: 'FAILED', error: 'push rejected' }]);
      await openPublish();
      expect(publishButton().disabled).toBe(false);
      expect(screen.queryByText(D.result.publishedAs('guardian/x', 'abc1234'))).toBeNull();
    });

    it('disables Publish while an action is still undecided', async () => {
      await openPublish([executed(), action({ id: 42, status: 'PROPOSED' })]);
      expect(publishButton().disabled).toBe(true);
      expect(screen.getByText((c) => c.includes(D.lib.stillOpen(1)))).toBeTruthy();
    });

    it('disables Publish when a held action is waiting for the gate', async () => {
      await openPublish([executed(), action({ id: 42, status: 'HELD', tool: 'jev_mcp:apply_patch' })]);
      expect(publishButton().disabled).toBe(true);
    });

    it('disables Publish when no file change was executed', async () => {
      await openPublish([action({ id: 42, tool: 'run_tests', status: 'EXECUTED' })]);
      expect(publishButton().disabled).toBe(true);
      expect(screen.getByText((c) => c.includes(D.lib.noFileChange))).toBeTruthy();
    });

    it('shows the hub plan in the confirm dialog before publishing', async () => {
      await openPublish();
      const dialog = await openDialog();
      expect(mocked.getPublishPlan).toHaveBeenCalledWith('thread_abc123', undefined);
      expect(within(dialog).getByText('guardian/thread_abc123', { selector: 'span' })).toBeTruthy();
      expect(within(dialog).getByText('0123456')).toBeTruthy();
      expect(within(dialog).getByText('origin')).toBeTruthy();
      expect(within(dialog).getByText('guardian: thread_abc123')).toBeTruthy();
      expect(within(dialog).getByText('app.py')).toBeTruthy();
      expect(within(dialog).getByText('lib/util.py')).toBeTruthy();
      expect(within(dialog).queryByText(D.result.branchExists)).toBeNull();
      expect((within(dialog).getByRole('button', { name: D.result.publish }) as HTMLButtonElement).disabled).toBe(false);
    });

    it('notes that the publish retries an existing branch', async () => {
      await openPublish();
      mocked.getPublishPlan.mockResolvedValue({ ...plan, retry: true });
      const dialog = await openDialog();
      expect(within(dialog).getByText(D.result.branchExists)).toBeTruthy();
    });

    it('shows the refusal and keeps Confirm disabled when the plan is refused', async () => {
      await openPublish();
      mocked.getPublishPlan.mockRejectedValue(new api.HubError('the hub workspace moved since the actions ran', 409));
      const dialog = await openDialog();
      expect(within(dialog).getByText('the hub workspace moved since the actions ran')).toBeTruthy();
      expect((within(dialog).getByRole('button', { name: D.result.publish }) as HTMLButtonElement).disabled).toBe(true);
      expect(mocked.publishThread).not.toHaveBeenCalled();
    });

    it('validates the branch name before enabling Confirm and asks for the plan of a valid one', async () => {
      await openPublish();
      const dialog = await openDialog();
      const confirm = () => within(dialog).getByRole('button', { name: D.result.publish }) as HTMLButtonElement;
      change(within(dialog).getByLabelText(D.result.branchOptional), '-bad name');
      await settle(400);
      expect(within(dialog).getByText(D.result.branchInvalid)).toBeTruthy();
      expect(confirm().disabled).toBe(true);

      mocked.getPublishPlan.mockResolvedValue({ ...plan, branch: 'feature/x' });
      change(within(dialog).getByLabelText(D.result.branchOptional), 'feature/x');
      await settle(400);
      expect(mocked.getPublishPlan).toHaveBeenLastCalledWith('thread_abc123', 'feature/x');
      expect(within(dialog).queryByText(D.result.branchInvalid)).toBeNull();
      expect(confirm().disabled).toBe(false);
    });

    it('publishes the thread after the confirm step and shows the pushed branch', async () => {
      mocked.publishThread.mockResolvedValue({
        ok: true, status: 'PUSHED', thread_id: 'thread_abc123', branch: 'feature/x', commit_sha: 'abcdef0123456789', files: ['app.py'],
      });
      await openPublish();
      const dialog = await openDialog();
      expect(mocked.publishThread).not.toHaveBeenCalled();
      change(within(dialog).getByLabelText(D.result.branchOptional), 'feature/x');
      await settle(400);
      fireEvent.click(within(dialog).getByRole('button', { name: D.result.publish }));
      await settle();
      expect(mocked.publishThread).toHaveBeenCalledWith('thread_abc123', 'feature/x');
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.getByText(D.result.pushed('feature/x', 'abcdef012345', 'app.py'))).toBeTruthy();
    });

    it('keeps the dialog open with the hub refusal when publishing is refused', async () => {
      mocked.publishThread.mockRejectedValue(new api.HubError('the project test failed (exit 1): go test ./...', 409));
      await openPublish();
      const dialog = await openDialog();
      fireEvent.click(within(dialog).getByRole('button', { name: D.result.publish }));
      await settle();
      expect(within(screen.getByRole('dialog', { name: D.result.publishThread })).getByText(/the project test failed/)).toBeTruthy();
    });

    it('names the real thread in the CLI hint when UI actions are disabled', async () => {
      mocked.publishThread.mockRejectedValue(new api.HubError('UI actions are disabled', 403));
      await openPublish();
      const dialog = await openDialog();
      fireEvent.click(within(dialog).getByRole('button', { name: D.result.publish }));
      await settle();
      expect(within(screen.getByRole('dialog', { name: D.result.publishThread })).getByText(/guardian\.py --publish-thread thread_abc123/)).toBeTruthy();
    });

    it('shows the push error of a failed publish', async () => {
      mocked.publishThread.mockResolvedValue({ ok: false, status: 'FAILED', branch: 'guardian/thread_abc123', stderr: 'remote: denied' });
      await openPublish();
      const dialog = await openDialog();
      fireEvent.click(within(dialog).getByRole('button', { name: D.result.publish }));
      await settle();
      expect(screen.getByRole('alert').textContent).toContain('remote: denied');
    });
  });

  it('shows a recheck failure with the hub guidance', async () => {
    mocked.jevRecheck.mockRejectedValue(new api.HubError('jev unreachable', 400));
    await openDone();
    fireEvent.click(screen.getByRole('button', { name: D.result.recheck }));
    await settle();
    expect(screen.getByText(D.lib.invalidRequest('jev unreachable'))).toBeTruthy();
  });

  it('opens a proposed action from the result on the Approvals page (no dialog on Debate)', async () => {
    await openDone();
    fireEvent.click(screen.getByRole('button', { name: D.result.openAction(41) }));
    await settle();
    expect(window.location.hash).toBe('#/approvals/41');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocked.getAction).not.toHaveBeenCalled();
  });

  it('continues a thread: sets the id and leaves the reference code empty', async () => {
    await openDone();
    fireEvent.click(screen.getByRole('button', { name: D.result.continueThread }));
    expect((screen.getByLabelText(D.start.continueThread) as HTMLInputElement).value).toBe('thread_abc123');
    expect((screen.getByLabelText(new RegExp('^' + D.result.refCode)) as HTMLTextAreaElement).value).toBe('');
    expect(screen.getByText(D.start.reuseThread)).toBeTruthy();
  });

  describe('run again', () => {
    beforeEach(() => {
      // The hub lists every team converted into a flow of the same id.
      mocked.fetchFlows.mockResolvedValue({
        flows: [{
          id: 'cross-review', name: 'Cross review', source: 'team', hash: 'h', calls: 3, runnable: true, run_error: null, errors: [], warnings: [],
          data: { id: 'cross-review', name: 'Cross review', seats: [], close: { character: 'judge' }, links: [] },
        }],
        catalog: {} as never,
      });
    });
    afterEach(() => {
      mocked.fetchFlows.mockResolvedValue({ flows: [], catalog: {} as never });
    });
    const openJob = async (job: DebateJobDetail) => {
      mocked.fetchDebates.mockResolvedValue([job]);
      mocked.fetchDebate.mockResolvedValue(job);
      await mount();
      // Closed debates are hidden from the list unless asked for.
      if (job.thread_closed) fireEvent.click(screen.getByLabelText(D.page.showClosed));
      follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
      await settle();
    };

    it('fills the form with the task, the flow of its team, rounds and reference code of a failed debate that left no result', async () => {
      await openJob({ ...doneJob, status: 'failed', exit_code: 1, error: 'boom', result: null });
      fireEvent.click(screen.getByRole('button', { name: D.job.runAgain }));
      expect((screen.getByLabelText(D.start.task) as HTMLTextAreaElement).value).toBe('Harden sshd');
      // Teams are listed as flows: run again picks the flow of the team it ran with.
      expect((screen.getByLabelText(ko.flows.start.label) as HTMLSelectElement).value).toBe('cross-review');
      expect((screen.getByLabelText(D.start.rounds) as HTMLInputElement).value).toBe('3');
      expect((screen.getByLabelText(new RegExp('^' + D.result.refCode)) as HTMLTextAreaElement).value).toBe('apps/agent/*.go');
      // A fresh run: a flow always starts a new thread, so there is no thread field (continuing is the other button).
      expect(screen.queryByLabelText(D.start.continueThread)).toBeNull();
    });

    it('is offered for a closed debate too, and starts a new job with the same inputs', async () => {
      await openJob({ ...doneJob, thread_closed: true });
      mocked.startDebate.mockResolvedValue({ ...runningJob, id: 'job2' });
      fireEvent.click(screen.getByRole('button', { name: D.job.runAgain }));
      fireEvent.click(screen.getByRole('button', { name: D.start.start }));
      await settle();
      expect(mocked.startDebate).toHaveBeenCalledWith(
        expect.objectContaining({ flow: 'cross-review', task: 'Harden sshd', rounds: 3, context_paths: ['apps/agent/*.go'] })
      );
      expect(mocked.startDebate.mock.calls[0][0].thread_id).toBeUndefined();
    });

    it('is not offered while the debate runs', async () => {
      await openJob({ ...runningJob, log: '', result: null });
      expect(screen.queryByRole('button', { name: D.job.runAgain })).toBeNull();
    });
  });

  it('reports a finished debate without a result file', async () => {
    mocked.fetchDebates.mockResolvedValue([doneJob]);
    mocked.fetchDebate.mockResolvedValue({ ...doneJob, result: null });
    await mount();
    follow(screen.getByRole('link', { name: ko.agents.list.openDebate('job1') }));
    await settle();
    expect(screen.getByText(D.job.noResult)).toBeTruthy();
  });
});


describe('Debate page layout', () => {
  it('is the list, the detail and the New debate form only', async () => {
    await mount();
    expect(screen.getByRole('region', { name: D.page.newDebate })).toBeTruthy();
    expect(screen.getByRole('region', { name: new RegExp('^' + D.page.listTitle + ' \\(') })).toBeTruthy();
    for (const gone of [D.project.section, D.persona.section, ko.ops.overview.approvals.what]) {
      expect(screen.queryByRole('region', { name: new RegExp(`^${gone}`) })).toBeNull();
    }
    expect(screen.getByLabelText(ko.shell.projectSwitcher.label)).toBeTruthy();
    expect(mocked.listActions).toHaveBeenCalledTimes(1);
  });
});

describe('Debate job URL and navigation', () => {
  it('opens #/debates/:jobId directly', async () => {
    window.location.hash = '#/debates/job1';
    mocked.fetchDebate.mockResolvedValue(doneJob);
    await mount();
    expect(mocked.fetchDebate).toHaveBeenCalledWith('job1');
    expect(mocked.fetchDebate).toHaveBeenCalledTimes(1);
    expect(within(screen.getByRole('region', { name: D.result.title })).getAllByText(D.decisionLabel.DISPUTED).length).toBeGreaterThan(0);
  });

  it('shows why a job from the URL could not be opened', async () => {
    window.location.hash = '#/debates/gone';
    mocked.fetchDebate.mockRejectedValue(new api.HubError('debate not found', 404));
    await mount();
    expect(screen.getByText(D.page.openFailed('gone', 'debate not found'))).toBeTruthy();
  });

  it('puts a started debate in the URL and shows it as the selected row', async () => {
    mocked.startDebate.mockResolvedValue(runningJob);
    await mount();
    change(screen.getByLabelText(D.start.task), 'Harden sshd');
    fireEvent.click(screen.getByRole('button', { name: D.start.start }));
    await settle();
    expect(window.location.hash).toBe('#/debates/job1');
    expect(screen.getAllByText(ko.agents.jobStatus.running).length).toBeGreaterThan(0);
  });

  it('keeps the watched job polling while the page is not shown and shows the result on return', async () => {
    mocked.startDebate.mockResolvedValue(runningJob);
    let current: DebateJobDetail = { ...runningJob, log: 'round 1 started', result: null };
    mocked.fetchDebate.mockImplementation(async () => current);
    function Harness() {
      const [onPage, setOnPage] = useState(true);
      return (
        <>
          <button onClick={() => setOnPage((v) => !v)}>toggle</button>
          {onPage ? <Routed /> : <div>another page</div>}
        </>
      );
    }
    render(
      <GuardianRoot>
        <AppStores>
          <Harness />
        </AppStores>
      </GuardianRoot>
    );
    await settle();
    fireEvent.click(screen.getByRole('button', { name: D.page.newDebate }));
    change(screen.getByLabelText(D.start.task), 'Harden sshd');
    fireEvent.click(screen.getByRole('button', { name: D.start.start }));
    await settle();

    fireEvent.click(screen.getByText('toggle'));
    expect(screen.getByText('another page')).toBeTruthy();
    // The job finishes while another page is open; the store keeps polling it.
    current = doneJob;
    await settle(3000);
    await settle(3000);
    fireEvent.click(screen.getByText('toggle'));
    await settle();
    expect(within(screen.getByRole('region', { name: D.result.title })).getAllByText(D.decisionLabel.DISPUTED).length).toBeGreaterThan(0);
    expect(screen.getByText(/Disable password login after verifying key access/)).toBeTruthy();
    const calls = mocked.fetchDebate.mock.calls.length;
    await settle(30_000);
    expect(mocked.fetchDebate.mock.calls.length).toBe(calls);
  });
});

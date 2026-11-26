import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { action, doneJob, runningJob } from './debateFixtures';
import { diagnosis, fleetSSH, metrics } from './fixtures';
import { ko } from '../src/i18n/ko';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { clearAuditCache, rememberAudit } from '../src/lib/auditCache';
import { PROJECT_STORAGE_KEY } from '../src/lib/project';
import { AppStores } from '../src/state/AppStores';
import { OverviewPage } from '../src/pages/OverviewPage';
import { getDebatePrefill } from '../src/state/debatePrefill';

const mocked = vi.mocked(api);
const renderPage = () =>
  render(
    <GuardianRoot>
      <AppStores>
        <OverviewPage />
      </AppStores>
    </GuardianRoot>
  );
/** The Overview page never audits by itself: the operator starts the fleet SSH audit from its card. */
const runAudit = async () => fireEvent.click(await screen.findByRole('button', { name: ko.ops.overview.ssh.runAudit }));
const card = async (title: string) => (await screen.findByRole('region', { name: title })) as HTMLElement;

describe('OverviewPage', () => {
  beforeEach(() => {
    clearAuditCache();
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchFleetSSHAudit.mockResolvedValue(fleetSSH);
    mocked.listActions.mockResolvedValue([]);
    mocked.fetchDebates.mockResolvedValue([]);
  });

  it('shows fleet reachability from the shared probe: counts, offline names as node links, measured time', async () => {
    renderPage();
    const c = await card(ko.ops.overview.reachability.title);
    await within(c).findByText(ko.ops.overview.reachability.nodesOnline);
    expect(c.textContent).toContain('1/ 2');
    expect(within(c).getByRole('link', { name: 'site-b-ec1' }).getAttribute('href')).toBe('#/fleet/nodes/b2');
    expect(within(c).getByText(new RegExp(`^${ko.ops.overview.reachability.measured}`))).toBeTruthy();
    // The primary next step opens Nodes filtered to the offline ones.
    expect(within(c).getByRole('link', { name: ko.ops.overview.reachability.openOffline }).getAttribute('href')).toBe('#/fleet/nodes?status=offline');
    expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(1);
  });

  it('counts command_failed as online and other classes as not online', async () => {
    mocked.diagnoseFleet.mockResolvedValue({
      ...diagnosis,
      results: diagnosis.results.map((r) => (r.id === 'b2' ? { ...r, reachability: 'auth_failed' as const } : { ...r, status: 'OFFLINE' as const, reachability: 'command_failed' as const })),
    });
    renderPage();
    const c = await card(ko.ops.overview.reachability.title);
    await within(c).findByText(ko.ops.overview.reachability.nodesOnline);
    expect(c.textContent).toContain('1/ 2');
    expect(within(c).getByRole('link', { name: 'site-b-ec1' })).toBeTruthy();
    expect(within(c).queryByRole('link', { name: 'site-a-arm1' })).toBeNull();
  });

  it('links plain Nodes when everything is online', async () => {
    mocked.diagnoseFleet.mockResolvedValue({ ...diagnosis, online_count: 2, results: diagnosis.results.map((r) => ({ ...r, status: 'ONLINE' as const })) });
    renderPage();
    const c = await card(ko.ops.overview.reachability.title);
    expect((await within(c).findByRole('link', { name: ko.ops.overview.reachability.openNodes })).getAttribute('href')).toBe('#/fleet/nodes');
    expect(within(c).getByText(ko.ops.overview.reachability.allReachable)).toBeTruthy();
  });

  it('Re-measure probes the fleet again', async () => {
    renderPage();
    const c = await card(ko.ops.overview.reachability.title);
    fireEvent.click(await within(c).findByRole('button', { name: ko.ops.overview.reachability.rerun }));
    await waitFor(() => expect(mocked.diagnoseFleet).toHaveBeenCalledTimes(2));
  });

  it('shows compact hub metrics and links to the full view on Nodes', async () => {
    renderPage();
    const c = await card(ko.ops.overview.hub.title);
    expect(await within(c).findByText('34.1')).toBeTruthy();
    expect(within(c).getByText('91.0')).toBeTruthy();
    expect(within(c).queryByText('Top processes')).toBeNull();
    expect(within(c).getByRole('link', { name: ko.ops.overview.hub.details }).getAttribute('href')).toBe('#/fleet/nodes');
  });

  it('summarises the fleet SSH audit (not the hub-only score) and links failing nodes and SSH audit', async () => {
    renderPage();
    await runAudit();
    const c = await card(ko.ops.overview.ssh.title);
    await within(c).findByText(ko.ops.overview.ssh.avgScore);
    expect(c.textContent).toContain('2/ 3');
    expect(c.textContent).toContain('62.5');
    expect(within(c).getByRole('link', { name: 'site-b-ec1' }).getAttribute('href')).toBe('#/fleet/nodes/b2');
    expect(within(c).getByRole('link', { name: ko.ops.overview.ssh.openAudit }).getAttribute('href')).toBe('#/fleet/ssh');
    expect(mocked.fetchSSHSecurity).not.toHaveBeenCalled();
    // Exactly one audit, the one the operator asked for.
    expect(mocked.fetchFleetSSHAudit).toHaveBeenCalledTimes(1);
    expect(mocked.fetchFleetSSHAudit).toHaveBeenCalledWith([], true);
  });

  it('titles the page with the glance heading', async () => {
    renderPage();
    expect(await screen.findByRole('region', { name: ko.ops.overview.glance.title })).toBeTruthy();
    expect(screen.queryByText('Needs attention')).toBeNull();
  });

  it('does not start a fleet SSH audit on open or on Refresh; offers Run audit instead', async () => {
    renderPage();
    const c = await card(ko.ops.overview.ssh.title);
    expect(within(c).getByRole('button', { name: ko.ops.overview.ssh.runAudit })).toBeTruthy();
    expect(within(c).getByText(ko.ops.overview.ssh.noRecent)).toBeTruthy();
    await card(ko.ops.overview.reachability.title);
    expect(mocked.fetchFleetSSHAudit).not.toHaveBeenCalled();
  });

  it('shows a fresh cached audit (run on the SSH audit tab) without asking the hub again', async () => {
    rememberAudit(fleetSSH);
    renderPage();
    const c = await card(ko.ops.overview.ssh.title);
    await within(c).findByText(ko.ops.overview.ssh.avgScore);
    expect(c.textContent).toContain('62.5');
    expect(within(c).queryByRole('button', { name: ko.ops.overview.ssh.runAudit })).toBeNull();
    expect(mocked.fetchFleetSSHAudit).not.toHaveBeenCalled();
  });

  it('ignores a cached audit older than 5 minutes', async () => {
    rememberAudit(fleetSSH, Date.now() - 6 * 60 * 1000);
    renderPage();
    const c = await card(ko.ops.overview.ssh.title);
    expect(within(c).getByRole('button', { name: ko.ops.overview.ssh.runAudit })).toBeTruthy();
  });

  it('counts only PROPOSED actions as awaiting a decision and mentions held ones apart', async () => {
    mocked.listActions.mockResolvedValue([
      action({ id: 1, status: 'PROPOSED', created_at: '2026-10-04T10:00:00Z' }),
      action({ id: 2, status: 'PROPOSED' }),
      action({ id: 3, status: 'HELD' }),
      action({ id: 4, status: 'EXECUTED' }),
      action({ id: 5, status: 'REJECTED' }),
    ]);
    renderPage();
    const c = await card(ko.ops.overview.approvals.title);
    await within(c).findByText(ko.ops.overview.approvals.awaiting);
    expect(within(c).getByText('2')).toBeTruthy();
    expect(within(c).getByText(ko.ops.overview.approvals.held(1))).toBeTruthy();
    expect(within(c).getByText(new RegExp(`^${ko.ops.overview.approvals.oldest}`))).toBeTruthy();
    expect(within(c).getByRole('link', { name: ko.ops.overview.approvals.review }).getAttribute('href')).toBe('#/approvals');
    expect(mocked.listActions).toHaveBeenCalledWith(undefined, 'server-guardian');
  });

  it('names the selected project on the project-scoped cards', async () => {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, 'svc');
    mocked.fetchProjects.mockResolvedValue({
      custom_dir: '/x',
      projects: [{ id: 'svc', source: 'custom', data: { id: 'svc', name: 'Billing service' }, errors: [] }],
    });
    renderPage();
    for (const title of [ko.ops.overview.approvals.title, ko.ops.overview.debates.title]) {
      const c = await card(title);
      expect(await within(c).findByText(ko.ops.overview.project('Billing service'))).toBeTruthy();
    }
    expect(within(await card(ko.ops.overview.reachability.title)).queryByText(new RegExp(`^${ko.ops.overview.project('')}`))).toBeNull();
  });

  it('shows zero approvals waiting when nothing is proposed', async () => {
    mocked.listActions.mockResolvedValue([action({ status: 'EXECUTED' })]);
    renderPage();
    const c = await card(ko.ops.overview.approvals.title);
    await within(c).findByText(ko.ops.overview.approvals.awaiting);
    expect(within(c).getByText('0')).toBeTruthy();
    expect(within(c).getByRole('link', { name: ko.ops.overview.approvals.open })).toBeTruthy();
  });

  it('lists the latest debates newest first, with status, and links to Debate', async () => {
    const jobs = Array.from({ length: 7 }, (_, i) => ({
      ...runningJob,
      id: `j${i}`,
      task: `task ${i}`,
      status: i === 6 ? ('running' as const) : ('completed' as const),
      started_at: `2026-10-0${i + 1}T10:00:00Z`,
    }));
    mocked.fetchDebates.mockResolvedValue([...jobs, { ...doneJob, id: 'old', task: 'oldest', started_at: '2026-09-01T10:00:00Z' }]);
    renderPage();
    const c = await card(ko.ops.overview.debates.title);
    await within(c).findByText('task 6');
    const rows = within(c).getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(rows).toHaveLength(5);
    expect(rows[0]).toContain(ko.debate.jobStatus.running);
    expect(rows[0]).toContain('task 6');
    expect(rows[4]).toContain('task 2');
    expect(within(c).queryByText('oldest')).toBeNull();
    expect(within(c).getByRole('link', { name: ko.ops.overview.debates.open }).getAttribute('href')).toBe('#/debates');
    expect(mocked.fetchDebates).toHaveBeenCalledWith('server-guardian');
  });

  it('shows the retrieval cost of a recent debate only when the run reports it', async () => {
    mocked.fetchDebates.mockResolvedValue([
      { ...doneJob, id: 'r1', task: 'with cost', started_at: '2026-10-02T10:00:00Z', retrieval: { results: 176, bytes: 2 * 1024 * 1024 } },
      { ...doneJob, id: 'r2', task: 'old run', started_at: '2026-10-01T10:00:00Z' },
    ]);
    renderPage();
    const c = await card(ko.ops.overview.debates.title);
    await within(c).findByText('with cost');
    const rows = within(c).getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(rows[0]).toContain(ko.common.retrieval(176, '2.0 MB'));
    expect(rows[1]).not.toContain(ko.common.retrieval(0, '').split(' ')[0]);
  });

  it('rows of recent debates link to the job, and the cards use the selected project and the shared approvals fetch', async () => {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, 'svc');
    mocked.fetchProjects.mockResolvedValue({
      custom_dir: '/x',
      projects: [
        { id: 'server-guardian', source: 'builtin', data: { id: 'server-guardian' }, errors: [] },
        { id: 'svc', source: 'custom', data: { id: 'svc' }, errors: [] },
      ],
    });
    mocked.fetchDebates.mockResolvedValue([{ ...doneJob, id: 'jobX', task: 'svc task' }]);
    mocked.listActions.mockResolvedValue([action({ id: 8, status: 'PROPOSED' })]);
    renderPage();
    const c = await card(ko.ops.overview.debates.title);
    expect((await within(c).findByRole('link', { name: 'svc task' })).getAttribute('href')).toBe('#/debates/jobX');
    await waitFor(() => expect(mocked.fetchDebates).toHaveBeenLastCalledWith('svc'));
    await waitFor(() => expect(mocked.listActions).toHaveBeenLastCalledWith(undefined, 'svc'));
    await within(await card(ko.ops.overview.approvals.title)).findByText(ko.ops.overview.approvals.awaiting);
    // One fetch feeds both the card and the nav badge (no separate request from the card).
    const forSvc = mocked.listActions.mock.calls.filter(([, project]) => project === 'svc');
    expect(forSvc).toHaveLength(1);
  });

  it('leaves code wiki build jobs out of the recent debates', async () => {
    mocked.fetchDebates.mockResolvedValue([
      { ...doneJob, id: 'wiki1', task: 'code wiki build: stale pages', kind: 'code_wiki' },
      { ...doneJob, id: 'job2', task: 'Harden sshd' },
    ]);
    renderPage();
    const c = await card(ko.ops.overview.debates.title);
    expect(await within(c).findByRole('link', { name: 'Harden sshd' })).toBeTruthy();
    expect(within(c).queryByText('code wiki build: stale pages')).toBeNull();
  });

  it('lists the latest quick reviews of a code project above its debates (verification layer V1c)', async () => {
    mocked.fetchProjects.mockResolvedValue({ custom_dir: '/x', projects: [
      { id: 'server-guardian', source: 'builtin', data: { id: 'server-guardian', name: 'SG' }, errors: [], status: { present: true } },
    ] });
    mocked.fetchReviewList.mockResolvedValue([{ id: 'review_20261123_101112_ab12cd', status: 'completed', label: 'REVIEW',
      note: 'advisory, no Judge', state: 'done', project: 'server-guardian', range: 'main~1..main',
      counts: { high: 1, medium: 0, low: 2, notes: 0 }, created: '2026-11-23T10:00:00Z' }]);
    renderPage();
    const c = await card(ko.ops.overview.debates.title);
    const reviews = await within(c).findByRole('region', { name: ko.ops.overview.debates.reviewsHeading });
    expect(within(reviews).getByRole('link', { name: 'main~1..main' }).getAttribute('href')).toBe('#/debates/review/review_20261123_101112_ab12cd');
    expect(within(reviews).getByText(ko.review.counts(1, 0, 2))).toBeTruthy();
    mocked.fetchReviewList.mockResolvedValue([]);
  });

  it('says so when there are no debates', async () => {
    renderPage();
    expect(await within(await card(ko.ops.overview.debates.title)).findByText(ko.ops.overview.debates.none)).toBeTruthy();
  });

  it('"Debate this" on reachability hands over a reachability-only task', async () => {
    renderPage();
    const c = await card(ko.ops.overview.reachability.title);
    fireEvent.click(await within(c).findByRole('button', { name: ko.ops.overview.debateThis }));
    expect(window.location.hash).toBe('#/debates');
    const prefill = getDebatePrefill();
    expect(prefill?.source).toBe('overview');
    expect(prefill?.task).toContain('Scope: fleet reachability only.');
    expect(prefill?.task).toContain('1/2 nodes online');
    expect(prefill?.task).toContain('Offline: site-b-ec1');
    expect(prefill?.task).not.toContain('SSH');
  });

  it('"Debate this" on Fleet SSH hands over the fleet audit findings only', async () => {
    renderPage();
    await runAudit();
    const c = await card(ko.ops.overview.ssh.title);
    fireEvent.click(await within(c).findByRole('button', { name: ko.ops.overview.debateThis }));
    const task = getDebatePrefill()?.task ?? '';
    expect(task).toContain('Scope: the fleet SSH audit only.');
    expect(task).toContain('Fleet SSH audit (scope: 2 of 3 nodes audited): average score 62.5/100, 1 with failing rules.');
    expect(task).toContain('- site-b-ec1 (score 40):');
    expect(task).toContain('Not audited: lonely (unreachable)');
    expect(task).not.toContain('Fleet diagnosis');
  });

  it('"Debate all findings" combines the fleet probe and the fleet SSH audit, with no hub-only score', async () => {
    renderPage();
    await runAudit();
    await screen.findByText(ko.ops.overview.ssh.avgScore);
    fireEvent.click(screen.getByRole('button', { name: ko.ops.overview.glance.debateAll }));
    const task = getDebatePrefill()?.task ?? '';
    expect(task).toContain('Fleet diagnosis (scope: all 2 nodes): 1/2 nodes online');
    expect(task).toContain('Fleet SSH audit (scope: 2 of 3 nodes audited)');
    expect(task).not.toContain("hub's own sshd");
    expect(mocked.fetchSSHSecurity).not.toHaveBeenCalled();
  });

  it('shows each failure on its own card and disables the top debate action with nothing to debate', async () => {
    mocked.diagnoseFleet.mockRejectedValue(new Error('diagnose exploded'));
    mocked.fetchFleetSSHAudit.mockRejectedValue(new Error('audit exploded'));
    mocked.fetchMetrics.mockRejectedValue(new Error('metrics exploded'));
    mocked.listActions.mockRejectedValue(new Error('actions exploded'));
    renderPage();
    await runAudit();
    expect(await screen.findByText('diagnose exploded')).toBeTruthy();
    expect(await screen.findByText('audit exploded')).toBeTruthy();
    expect(await screen.findByText('metrics exploded')).toBeTruthy();
    expect(await screen.findByText('actions exploded')).toBeTruthy();
    expect((screen.getByRole('button', { name: ko.ops.overview.glance.debateAll }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: ko.ops.overview.debateThis })).toBeNull();
  });

  it('with one source down, the combined task names the unavailable source and its reason', async () => {
    mocked.diagnoseFleet.mockRejectedValue(new Error('probe timeout'));
    renderPage();
    await runAudit();
    await screen.findByText('probe timeout');
    await screen.findByText(ko.ops.overview.ssh.avgScore);
    fireEvent.click(screen.getByRole('button', { name: ko.ops.overview.glance.debateAll }));
    expect(getDebatePrefill()?.task).toContain('Fleet diagnosis: not available (probe timeout).');
  });

  it('has no hardcoded opinion or approve action of its own', async () => {
    const { container } = renderPage();
    await runAudit();
    await screen.findByText(ko.ops.overview.ssh.avgScore);
    const text = container.textContent ?? '';
    for (const forbidden of ['AI 종합 의견', 'Headscale', '100.64.0', 'RemediationAgent']) expect(text).not.toContain(forbidden);
    expect(screen.queryByRole('button', { name: /approve|run action/i })).toBeNull();
  });
});

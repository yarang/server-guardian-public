import { tr } from '../i18n';
import { isOnline } from '../lib/reachability';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Panel, StatCard, StatusChip } from '@server-guardian/ui';
import { fetchFleetSSHAudit, fetchMetrics, fetchReviewList } from '../api';
import { Ago } from '../components/Ago';
import { DataGate, ErrorNotice, LoadingNote } from '../components/DataGate';
import { Grid, Row, Stack, mono, muted } from '../components/layout';
import { useRefreshToken } from '../hooks/refresh';
import { useHubAction } from '../hooks/useHubAction';
import { useHubData } from '../hooks/useHubData';
import { isAwaitingDecision, isHeld, parseTime, jobStatusTone, visibleJobs } from '../lib/debate';
import { freshAudit, rememberAudit } from '../lib/auditCache';
import { buildDebateTask } from '../lib/debateTask';
import type { DebateScope } from '../lib/debateTask';
import { fixed, retrievalText } from '../lib/format';
import { scoreTone, usageTone } from '../lib/tones';
import { fleetHref, hrefFor, navigate } from '../router';
import { useApprovalsStore } from '../state/approvalsStore';
import { isWikiJob } from '../lib/wiki';
import { hasNoRepository } from '../lib/project';
import { reviewParams } from '../lib/review';
import { setDebatePrefill } from '../state/debatePrefill';
import { useDebateStore } from '../state/debateStore';
import { useProjectStore } from '../state/projectStore';
import { useHubStore } from '../state/hubStore';
import type { FleetSSHReport } from '../types';

/** Number of recent debates listed on the card. */
const RECENT_LIMIT = 5;

/** Name of the selected project: the Approvals and Debates cards only show that project's records. */
function ProjectNote() {
  const { selected, projects } = useProjectStore();
  const name = projects.data?.projects.find((p) => p.id === selected)?.data.name || selected;
  return <div style={{ ...muted, fontSize: 12 }}>{tr.ops.overview.project(name)}</div>;
}

/** A card with its one primary next step in the footer and, optionally, a secondary action. */
function CardFooter({ children }: { children: ReactNode }) {
  return <Row gap="var(--sg-s-6)">{children}</Row>;
}

/** Node names as links to their detail pages. */
function NodeLinks({ nodes }: { nodes: Array<{ id: string; name: string }> }) {
  return (
    <>
      {nodes.map((n, i) => (
        <span key={n.id}>
          {i > 0 ? ', ' : ''}
          <a href={fleetHref('nodes', [n.id])}>{n.name}</a>
        </span>
      ))}
    </>
  );
}

function ReachabilityCard({ onDebate }: { onDebate: () => void }) {
  const { diagnosis } = useHubStore();
  return (
    <Panel
      title={tr.ops.overview.reachability.title}
      actions={
        <Button size="sm" variant="outline" loading={diagnosis.loading} onClick={diagnosis.rerun}>
          {tr.ops.overview.reachability.rerun}
        </Button>
      }
    >
      <DataGate state={diagnosis} what={tr.ops.overview.reachability.what}>
        {(report) => {
          const results = report.results ?? [];
          const online = results.length ? results.filter(isOnline).length : report.online_count;
          const offline = results.filter((r) => !isOnline(r));
          return (
            <Stack gap="var(--sg-s-6)">
              <StatCard
                label={tr.ops.overview.reachability.nodesOnline}
                value={online}
                unit={`/ ${report.total_nodes}`}
                caption={offline.length ? tr.ops.overview.reachability.notOnline(offline.length) : tr.ops.overview.reachability.allReachable}
                tone={offline.length ? 'crit' : 'ok'}
              />
              {offline.length ? (
                <div style={{ fontSize: 13 }}>
                  {tr.ops.overview.reachability.offline}
                  <NodeLinks nodes={offline} />
                </div>
              ) : null}
              {diagnosis.measuredAt !== null ? (
                <div style={{ ...muted, fontSize: 12 }}>
                  <Ago at={diagnosis.measuredAt} prefix={tr.ops.overview.reachability.measured} />
                </div>
              ) : null}
              <CardFooter>
                <a href={offline.length ? fleetHref('nodes', [], { status: 'offline' }) : fleetHref('nodes')}>
                  {offline.length ? tr.ops.overview.reachability.openOffline : tr.ops.overview.reachability.openNodes}
                </a>
                <Button size="sm" variant="ghost" onClick={onDebate}>
                  {tr.ops.overview.debateThis}
                </Button>
              </CardFooter>
            </Stack>
          );
        }}
      </DataGate>
    </Panel>
  );
}

/** Compact hub metrics (polled every 5 s); the full view with processes is on Nodes. */
function HubCard() {
  const metrics = useHubData(fetchMetrics, { pollMs: 5000 });
  return (
    <Panel title={tr.ops.overview.hub.title}>
      <DataGate state={metrics} what={tr.ops.overview.hub.what}>
        {(m) => (
          <Stack gap="var(--sg-s-6)">
            <Grid min={110} gap="var(--sg-s-5)">
              <StatCard label={tr.ops.overview.hub.cpu} value={fixed(m.cpu_usage_pct, 1)} unit="%" tone={usageTone(m.cpu_usage_pct)} />
              <StatCard label={tr.ops.overview.hub.memory} value={fixed(m.memory_pct, 1)} unit="%" tone={usageTone(m.memory_pct)} />
              <StatCard label={tr.ops.overview.hub.disk} value={fixed(m.disk_pct, 1)} unit="%" tone={usageTone(m.disk_pct)} />
              <StatCard label={tr.ops.overview.hub.load1m} value={fixed((m.load_avg ?? [])[0], 2)} />
            </Grid>
            <CardFooter>
              <a href={fleetHref('nodes')}>{tr.ops.overview.hub.details}</a>
            </CardFooter>
          </Stack>
        )}
      </DataGate>
    </Panel>
  );
}

function SshCard({
  audit,
  onRun,
  onDebate,
}: {
  audit: { data: FleetSSHReport | null; error: string | null; running: boolean };
  onRun: () => void;
  onDebate: () => void;
}) {
  const report = audit.data;
  return (
    <Panel title={tr.ops.overview.ssh.title}>
      {audit.error !== null ? (
        <ErrorNotice title={tr.ops.overview.ssh.auditFailed} message={audit.error} onRetry={onRun} retrying={audit.running} />
      ) : report === null ? (
        <Stack gap="var(--sg-s-4)">
          <div style={{ ...muted, fontSize: 13 }}>
            {audit.running ? tr.ops.overview.ssh.probing : tr.ops.overview.ssh.noRecent}
          </div>
          <div>
            <Button size="sm" variant="primary" loading={audit.running} onClick={onRun}>
              {tr.ops.overview.ssh.runAudit}
            </Button>
          </div>
        </Stack>
      ) : report.summary.nodes === 0 ? (
        <div style={muted}>{tr.ops.overview.ssh.noNodes}</div>
      ) : (
        <Stack gap="var(--sg-s-6)">
          <Grid min={110} gap="var(--sg-s-5)">
            <StatCard
              label={tr.ops.overview.ssh.audited}
              value={report.summary.audited}
              unit={`/ ${report.summary.nodes}`}
              tone={report.summary.unreachable || report.summary.errors ? 'crit' : 'ok'}
            />
            <StatCard
              label={tr.ops.overview.ssh.avgScore}
              value={report.summary.avg_score ?? '—'}
              unit={report.summary.avg_score === null ? undefined : '/ 100'}
              tone={report.summary.avg_score === null ? 'idle' : scoreTone(report.summary.avg_score)}
            />
          </Grid>
          {report.summary.failing_nodes.length ? (
            <div style={{ fontSize: 13 }}>
              {tr.ops.overview.ssh.failing}
              <NodeLinks nodes={report.nodes.filter((n) => report.summary.failing_nodes.includes(n.id))} />
            </div>
          ) : (
            <div style={{ ...muted, fontSize: 13 }}>{tr.ops.overview.ssh.noFailing}</div>
          )}
          {parseTime(report.generated_at) !== null ? (
            <div style={{ ...muted, fontSize: 12 }}>
              <Ago at={parseTime(report.generated_at) as number} prefix={tr.ops.overview.ssh.auditedAt} />
              {report.cached ? tr.ops.overview.ssh.hubCache : ''}
            </div>
          ) : null}
          <CardFooter>
            <a href={fleetHref('ssh')}>{tr.ops.overview.ssh.openAudit}</a>
            <Button size="sm" variant="ghost" onClick={onDebate}>
              {tr.ops.overview.debateThis}
            </Button>
          </CardFooter>
        </Stack>
      )}
    </Panel>
  );
}

/** Reads the shared approvals store (the same fetch as the nav badge) for the selected project. */
function ApprovalsCard() {
  const { actions: list, error, loading, refresh } = useApprovalsStore();
  const state = { data: list, error, loading, reload: refresh };
  return (
    <Panel title={tr.ops.overview.approvals.title}>
      <ProjectNote />
      <DataGate state={state} what={tr.ops.overview.approvals.what}>
        {(list) => {
          const waiting = list.filter(isAwaitingDecision);
          const held = list.filter(isHeld).length;
          const oldest = waiting.map((a) => parseTime(a.created_at)).filter((t): t is number => t !== null).sort((a, b) => a - b)[0];
          return (
            <Stack gap="var(--sg-s-6)">
              <StatCard
                label={tr.ops.overview.approvals.awaiting}
                value={waiting.length}
                caption={held ? tr.ops.overview.approvals.held(held) : tr.ops.overview.approvals.proposedByJudge}
                tone={waiting.length ? 'warn' : 'ok'}
              />
              {oldest !== undefined ? (
                <div style={{ ...muted, fontSize: 12 }}>
                  <Ago at={oldest} prefix={tr.ops.overview.approvals.oldest} />
                </div>
              ) : null}
              <CardFooter>
                <a href={hrefFor('approvals')}>{waiting.length ? tr.ops.overview.approvals.review : tr.ops.overview.approvals.open}</a>
              </CardFooter>
            </Stack>
          );
        }}
      </DataGate>
    </Panel>
  );
}

const REVIEW_LIMIT = 3;

/**
 * The selected code project's latest quick reviews (docs/design/verification-layer.md, V1c): change, state and
 * findings by severity; rows open `#/debates/review/:id`. Nothing is shown while the project has none.
 */
function RecentReviews({ project }: { project: string }) {
  const reviews = useHubData(() => fetchReviewList(project));
  const D = tr.ops.overview.debates;
  const rows = (reviews.data ?? []).slice(0, REVIEW_LIMIT);
  if (reviews.error || !rows.length) return null;
  return (
    <section aria-label={D.reviewsHeading} style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
      <div style={{ ...muted, fontSize: 12, fontWeight: 600 }}>{D.reviewsHeading}</div>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--sg-s-4)' }}>
        {rows.map((r) => {
          const when = r.created ? parseTime(r.created) : null;
          const name = r.range ?? (r.patch ? tr.review.patchOn((r.head ?? '').slice(0, 12)) : r.id);
          return (
            <li key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--sg-s-4)', minWidth: 0, fontSize: 13 }}>
              <StatusChip tone={r.counts?.high ? 'crit' : jobStatusTone(r.status)} title={r.status}>
                {r.label}
              </StatusChip>
              <a href={hrefFor('debates', reviewParams({ id: r.id }))} style={{ ...mono, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={name}>
                {name}
              </a>
              {r.counts && r.state === 'done' ? (
                <span style={{ ...muted, fontSize: 12, whiteSpace: 'nowrap' }}>{tr.review.counts(r.counts.high, r.counts.medium, r.counts.low)}</span>
              ) : null}
              {when !== null ? (
                <span style={{ ...mono, ...muted, fontSize: 12, whiteSpace: 'nowrap' }}>
                  <Ago at={when} />
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>
      <div style={{ fontSize: 13 }}>
        <a href={hrefFor('debates', reviewParams({}))}>{D.openReviews}</a>
      </div>
    </section>
  );
}

/**
 * Recent checks of the selected project: its quick reviews (code projects) and its debates, from the shared debate
 * store; rows open the review or `#/debates/:id`.
 */
function DebatesCard() {
  const { recent } = useDebateStore();
  const { selected, projects } = useProjectStore();
  const code = !hasNoRepository(projects.data?.projects.find((p) => p.id === selected));
  return (
    <Panel title={tr.ops.overview.debates.title}>
      <ProjectNote />
      {code ? <RecentReviews key={selected} project={selected} /> : null}
      <DataGate state={recent} what={tr.ops.overview.debates.what}>
        {(jobs) => {
          const recent = visibleJobs(jobs.filter((j) => !isWikiJob(j)), false).sort((a, b) => (parseTime(b.started_at) ?? 0) - (parseTime(a.started_at) ?? 0)).slice(0, RECENT_LIMIT);
          return (
            <Stack gap="var(--sg-s-5)">
              {recent.length === 0 ? (
                <div style={{ ...muted, fontSize: 13 }}>{tr.ops.overview.debates.none}</div>
              ) : (
                <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--sg-s-4)' }}>
                  {recent.map((j) => {
                    const when = parseTime(j.started_at);
                    return (
                      <li key={j.id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--sg-s-4)', minWidth: 0, fontSize: 13 }}>
                        <StatusChip tone={jobStatusTone(j.status)} title={j.status.toUpperCase()}>{tr.debate.jobStatus[j.status] ?? j.status.toUpperCase()}</StatusChip>
                        <a
                          href={hrefFor('debates', [j.id])}
                          style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                          title={j.task}
                        >
                          {j.task}
                        </a>
                        {j.retrieval ? (
                          <span style={{ ...mono, ...muted, fontSize: 12, whiteSpace: 'nowrap' }}>{retrievalText(j.retrieval)}</span>
                        ) : null}
                        {when !== null ? (
                          <span style={{ ...mono, ...muted, fontSize: 12, whiteSpace: 'nowrap' }}>
                            <Ago at={when} />
                          </span>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              )}
              <CardFooter>
                <a href={hrefFor('debates')}>{tr.ops.overview.debates.open}</a>
              </CardFooter>
            </Stack>
          );
        }}
      </DataGate>
    </Panel>
  );
}

/**
 * Overview (the landing destination): what needs attention now. Every card shows measured facts only (the shared fleet
 * diagnosis, hub metrics, the fleet SSH audit, pending approvals, recent debates) and one next step.
 * It generates no opinion itself; "Debate this" hands a task scoped to the card to the Debates page.
 */
export function OverviewPage() {
  const { diagnosis } = useHubStore();
  const fetched = useHubAction(fetchFleetSSHAudit);
  const refreshToken = useRefreshToken();
  const run = fetched.run;
  // The fleet audit SSHes into every node, so the Overview never starts one by itself: it shows the audit from the
  // last 5 minutes (run here or on the SSH audit tab) and otherwise offers "Run audit".
  const [cachedReport, setCachedReport] = useState(() => freshAudit());
  useEffect(() => setCachedReport(freshAudit()), [refreshToken]);
  const fetchedReport = fetched.data;
  useEffect(() => {
    if (fetchedReport) rememberAudit(fetchedReport);
  }, [fetchedReport]);
  const audit = { data: fetchedReport ?? cachedReport, error: fetched.error, running: fetched.running };

  const debate = (scope: DebateScope) => {
    const task = buildDebateTask({
      scope,
      diagnosis: diagnosis.data,
      diagnosisError: diagnosis.error,
      fleetSsh: audit.data,
      fleetSshError: audit.error,
    });
    setDebatePrefill(task, 'overview', {});
    navigate('debates');
  };
  const hasData = diagnosis.data !== null || audit.data !== null;

  return (
    <Stack>
      <Panel
        title={tr.ops.overview.glance.title}
        actions={
          <Button variant="primary" size="sm" disabled={!hasData} onClick={() => debate('all')}>
            {tr.ops.overview.glance.debateAll}
          </Button>
        }
      >
        <p style={{ margin: 0, color: 'var(--sg-text-secondary)', fontSize: 13 }}>
          {tr.ops.overview.glance.intro}
        </p>
      </Panel>
      <Grid min={320}>
        <ReachabilityCard onDebate={() => debate('reachability')} />
        <SshCard audit={audit} onRun={() => void run([], true)} onDebate={() => debate('ssh')} />
        <ApprovalsCard />
        <DebatesCard />
        <HubCard />
      </Grid>
    </Stack>
  );
}

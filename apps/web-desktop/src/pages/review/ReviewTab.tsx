import { useState } from 'react';
import { Button, Icon, Panel, SegmentedControl, StatusChip, TextField } from '@server-guardian/ui';
import { AuthRequiredError, fetchPersonas, fetchReview, fetchReviewList, fetchTeams, startReview } from '../../api';
import { Ago } from '../../components/Ago';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Disclosure } from '../../components/Disclosure';
import { MonoBlock } from '../../components/MonoBlock';
import { Notice } from '../../components/Notice';
import { ProjectSwitcher } from '../../components/ProjectSwitcher';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { TASK_MAX, jobStatusTone, parseTime } from '../../lib/debate';
import { errorText } from '../../lib/format';
import {
  REVIEW_LIST_POLL_MS,
  REVIEW_PATCH_MAX,
  REVIEW_POLL_MS,
  SEVERITIES,
  debateTaskFrom,
  findingsBySeverity,
  parseReviewRoute,
  patchBytes,
  reviewParams,
  reviewRoles,
  severityTone,
  validRange,
} from '../../lib/review';
import { soloProviders } from '../../lib/solo';
import { requestChangeName, reviewChangeName, reviewListName, validBranch } from './source';
import { hrefFor, navigate } from '../../router';
import { setDebatePrefill } from '../../state/debatePrefill';
import { useProjectStore } from '../../state/projectStore';
import type { ReviewFinding, ReviewListItem, ReviewStart, ReviewView } from '../../types';
import { tr } from '../../i18n';

const R = () => tr.review;

const rowStyle = {
  display: 'grid',
  gap: 'var(--sg-s-2)',
  padding: 'var(--sg-s-4) var(--sg-s-5)',
  background: 'var(--sg-surface-raised)',
  color: 'var(--sg-text)',
  border: '1px solid var(--sg-border)',
  borderRadius: 'var(--sg-r-md)',
  fontSize: 13,
  textDecoration: 'none',
  minWidth: 0,
} as const;

const metaStyle = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-3)' } as const;

/** One recent review: the change (range, patch, branch or action), status, findings by severity and age. */
function ReviewRow({ item }: { item: ReviewListItem }) {
  const created = item.created ? parseTime(item.created) : null;
  const name = reviewListName(item);
  return (
    <a href={hrefFor('debates', reviewParams({ id: item.id }))} aria-label={R().openRun(name)} style={rowStyle}>
      <span style={{ ...mono, overflowWrap: 'anywhere' }}>{name}</span>
      <span style={metaStyle}>
        <StatusChip tone={jobStatusTone(item.status)} title={item.status}>
          {tr.agents.jobStatus[item.status] ?? item.status}
        </StatusChip>
        {item.state && item.state !== 'done' ? <StatusChip tone="warn">{R().stateLabel[item.state] ?? item.state}</StatusChip> : null}
        {item.counts && item.state === 'done' ? <span style={{ ...muted, fontSize: 12 }}>{R().counts(item.counts.high, item.counts.medium, item.counts.low)}</span> : null}
        {item.role ? <span style={{ ...mono, ...muted, fontSize: 12 }}>{item.role}</span> : null}
        {item.started_by ? (
          <span style={{ ...mono, ...muted, fontSize: 12 }} title={item.started_by}>
            {item.started_by}
          </span>
        ) : null}
        {created !== null ? (
          <span style={{ ...muted, fontSize: 12 }}>
            <Ago at={created} />
          </span>
        ) : null}
      </span>
    </a>
  );
}

/**
 * The form: a commit range, a pasted patch or a branch of the selected code project's origin (V2a), an optional
 * reviewer and provider. A 202 hands the run to the detail view; the hub's 400/429 message stays here.
 */
function ReviewForm({ project }: { project: string }) {
  const personas = useHubData(fetchPersonas);
  const teams = useHubData(fetchTeams);
  const [mode, setMode] = useState<'range' | 'patch' | 'ref'>('range');
  const [range, setRange] = useState('');
  const [patch, setPatch] = useState('');
  const [patchBase, setPatchBase] = useState('');
  const [branch, setBranch] = useState('');
  const [branchBase, setBranchBase] = useState('');
  const [role, setRole] = useState('');
  const [provider, setProvider] = useState('');
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);

  const onStart = async () => {
    if (starting) return;
    const req: ReviewStart = { project };
    if (mode === 'range') {
      if (!validRange(range)) return setError(R().badRange);
      req.range = range.trim();
    } else if (mode === 'ref') {
      if (!validBranch(branch)) return setError(R().badBranch);
      req.ref = branch.trim();
      if (branchBase.trim()) {
        if (!validBranch(branchBase)) return setError(R().badBranch);
        req.base = branchBase.trim();
      }
    } else {
      if (!patch.trim()) return setError(R().emptyPatch);
      if (patchBytes(patch) > REVIEW_PATCH_MAX) return setError(R().patchTooLarge);
      req.patch = patch.endsWith('\n') ? patch : `${patch}\n`;
      if (patchBase.trim()) req.patch_base = patchBase.trim();
    }
    if (role) req.role = role;
    if (provider) req.provider = provider;
    setStarting(true);
    setError('');
    try {
      const started = await startReview(req);
      navigate('debates', reviewParams({ id: started.id }));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
    } finally {
      setStarting(false);
    }
  };

  // minmax(0, 1fr): a long option text in a select must not widen the form past its card (checked at 390 px).
  return (
    <Stack gap="var(--sg-s-6)" style={{ gridTemplateColumns: 'minmax(0, 1fr)' }}>
      <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>{R().intro}</div>
      <SegmentedControl
        aria-label={R().mode}
        options={[
          { value: 'range', label: R().modeRange },
          { value: 'patch', label: R().modePatch },
          { value: 'ref', label: R().modeBranch },
        ]}
        value={mode}
        onChange={(v) => {
          setMode(v === 'patch' ? 'patch' : v === 'ref' ? 'ref' : 'range');
          setError('');
        }}
      />
      {mode === 'range' ? (
        <TextField label={R().range} mono placeholder="main~1..main" hint={R().rangeHint} value={range} onChange={(e) => setRange(e.target.value)} />
      ) : mode === 'ref' ? (
        <>
          <TextField label={R().branch} mono placeholder="feature/x" hint={R().branchHint} value={branch} onChange={(e) => setBranch(e.target.value)} />
          <TextField label={R().branchBase} mono hint={R().branchBaseHint} value={branchBase} onChange={(e) => setBranchBase(e.target.value)} />
        </>
      ) : (
        <>
          <TextAreaField label={R().patch} mono rows={10} hint={R().patchHint} value={patch} onChange={(e) => setPatch(e.target.value)} />
          <TextField label={R().patchBase} mono placeholder="HEAD" value={patchBase} onChange={(e) => setPatchBase(e.target.value)} />
        </>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 220px), 1fr))', gap: 'var(--sg-s-6)', alignItems: 'start' }}>
        <SelectField
          label={R().role}
          value={role}
          onChange={(e) => setRole(e.target.value)}
          options={[
            { value: '', label: R().roleDefault },
            ...reviewRoles(personas.data?.personas ?? []).map((p) => ({ value: p.id, label: `${p.name || p.id} (${p.id})` })),
          ]}
        />
        <SelectField
          label={R().provider}
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          options={[
            { value: '', label: R().providerDefault },
            ...soloProviders(teams.data?.catalog?.installed, false).map((p) => ({ value: p, label: p })),
          ]}
        />
      </div>
      <div>
        <Button variant="primary" icon={<Icon name="check" size={14} />} loading={starting} disabled={starting} onClick={() => void onStart()}>
          {starting ? R().starting : R().start}
        </Button>
      </div>
      {error ? <Notice tone="crit">{error}</Notice> : null}
    </Stack>
  );
}

function FindingItem({ finding }: { finding: ReviewFinding }) {
  return (
    <li style={{ display: 'grid', gap: 'var(--sg-s-2)', overflowWrap: 'anywhere' }}>
      <Row>
        <StatusChip tone={severityTone(finding.severity)}>{R().severity[finding.severity] ?? finding.severity}</StatusChip>
        <span style={{ ...mono, fontSize: 12 }}>{`${finding.path}:${finding.line}`}</span>
      </Row>
      <span>{finding.claim}</span>
      {finding.quote ? <MonoBlock text={finding.quote} maxHeight={120} /> : null}
      {finding.scenario ? (
        <span style={{ ...muted, fontSize: 12 }}>
          {R().scenario}: {finding.scenario}
        </span>
      ) : null}
    </li>
  );
}

/** One review: the REVIEW label with its "no Judge" line, the change, the findings by severity, the notes. */
function ReviewRunView({ view }: { view: ReviewView }) {
  const result = view.result;
  // While it runs the stored request already names the change (no shas in it yet).
  const requestName = requestChangeName(view.request?.source);
  const [notesOpen, setNotesOpen] = useState(false);
  const groups = result ? findingsBySeverity(result.findings) : null;
  const runAsDebate = () => {
    if (!result) return;
    setDebatePrefill(debateTaskFrom(result, R().debateIntro, R().debateHeading, TASK_MAX), 'review');
    navigate('debates');
  };
  return (
    <Stack gap="var(--sg-s-5)">
      <div>
        <a href={hrefFor('debates', reviewParams({}))}>{R().back}</a>
      </div>
      <Card>
        <Row>
          <StatusChip tone="idle">{view.label}</StatusChip>
          <span style={{ ...muted, fontSize: 12 }}>{R().note}</span>
          <StatusChip tone={jobStatusTone(view.status)} title={view.status}>
            {tr.agents.jobStatus[view.status] ?? view.status}
          </StatusChip>
          {view.request?.started_by ? (
            <span style={{ ...mono, ...muted, fontSize: 12 }} title={view.request.started_by}>
              {view.request.started_by}
            </span>
          ) : null}
        </Row>
        <div style={{ ...muted, fontSize: 12 }}>{R().noJudge}</div>
        {result ? (
          <>
            <strong style={{ ...mono, overflowWrap: 'anywhere' }}>{reviewChangeName(result)}</strong>
            <Row>
              <span style={{ ...muted, fontSize: 12 }}>{R().stats(result.files.length, result.added, result.removed)}</span>
              <span style={{ ...mono, ...muted, fontSize: 12 }}>{R().roleLabel(result.role)}</span>
              <span style={{ ...mono, ...muted, fontSize: 12 }}>{R().providerLabel(result.provider)}</span>
              <span style={{ ...muted, fontSize: 12 }}>{R().elapsed(result.elapsed_s)}</span>
            </Row>
          </>
        ) : null}
        {view.status === 'running' ? (
          <Stack gap="var(--sg-s-2)">
            {requestName ? <strong style={{ ...mono, overflowWrap: 'anywhere' }}>{requestName}</strong> : null}
            <div style={{ ...muted, fontSize: 13 }}>{R().waiting}</div>
          </Stack>
        ) : null}
        {view.status === 'failed' ? <Notice tone="crit">{view.error || R().failed}</Notice> : null}
        {result?.state === 'degraded' ? <Notice tone="warn">{R().degraded}</Notice> : null}
        {result?.state === 'bad_format' ? <Notice tone="warn">{R().badFormat}</Notice> : null}
        {result ? (
          <div>
            <Button size="sm" variant="outline" onClick={runAsDebate}>
              {R().runAsDebate}
            </Button>
          </div>
        ) : null}
      </Card>
      {result && result.state === 'done' && groups ? (
        <Panel title={result.summary}>
          {result.findings.length === 0 ? (
            <div style={{ ...muted, fontSize: 13 }}>{R().none}</div>
          ) : (
            <Stack gap="var(--sg-s-6)">
              {SEVERITIES.filter((s) => groups[s].length).map((s) => (
                <section key={s} aria-label={R().severityGroup(R().severity[s] ?? s, groups[s].length)}>
                  <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-6)', display: 'grid', gap: 'var(--sg-s-5)' }}>
                    {groups[s].map((f, i) => (
                      <FindingItem key={`${f.path}:${f.line}:${i}`} finding={f} />
                    ))}
                  </ul>
                </section>
              ))}
            </Stack>
          )}
        </Panel>
      ) : null}
      {result && result.notes.length ? (
        <Disclosure summary={R().notesTitle(result.notes.length)} open={notesOpen} onToggle={setNotesOpen}>
          <div style={{ ...muted, fontSize: 12 }}>{R().notesHint}</div>
          <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-6)', display: 'grid', gap: 'var(--sg-s-4)' }}>
            {result.notes.map((n, i) => (
              <li key={`${n.path}:${n.line}:${i}`} style={{ overflowWrap: 'anywhere', fontSize: 13 }}>
                <span style={{ ...mono, fontSize: 12 }}>{`${n.path || '?'}:${n.line}`}</span> {n.claim}{' '}
                <span style={{ ...muted, fontSize: 12 }}>({n.reason})</span>
              </li>
            ))}
          </ul>
        </Disclosure>
      ) : null}
      {result?.text ? <MonoBlock text={result.text} maxHeight={400} /> : null}
    </Stack>
  );
}

function useReviewRun(id: string) {
  const [status, setStatus] = useState('');
  return useHubData(
    () =>
      fetchReview(id).then((view) => {
        setStatus(view.status);
        return view;
      }),
    { pollMs: status === 'running' ? REVIEW_POLL_MS : undefined },
  );
}

function ReviewDetail({ id }: { id: string }) {
  const state = useReviewRun(id);
  return <DataGate state={state} what={R().what}>{(view) => <ReviewRunView view={view} />}</DataGate>;
}

/** The selected project's recent reviews; remounted (by key) when the project changes. */
function RecentReviews({ project }: { project: string }) {
  const recent = useHubData(() => fetchReviewList(project), { pollMs: REVIEW_LIST_POLL_MS });
  return (
    <Panel title={R().recentTitle}>
        <DataGate state={recent} what={R().listWhat}>
          {(runs) =>
            runs.length === 0 ? (
              <div style={{ ...muted, fontSize: 13 }}>{R().empty}</div>
            ) : (
              <nav aria-label={R().listAria} style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
                {runs.map((run) => (
                  <ReviewRow key={run.id} item={run} />
                ))}
              </nav>
            )
          }
        </DataGate>
    </Panel>
  );
}

function ReviewHome() {
  const { selected } = useProjectStore();
  return (
    <Stack>
      <ProjectSwitcher />
      <Card>
        <ReviewForm project={selected} />
      </Card>
      <RecentReviews key={selected} project={selected} />
    </Stack>
  );
}

/**
 * Quick review (docs/design/verification-layer.md, V1c): one reviewer checks one change of a code project, labelled
 * REVIEW, no Judge. `#/debates/review` is the form and the recent reviews; `#/debates/review/<id>` follows one.
 * V2e adds the Branch change mode; the approvals card starts action reviews (ActionReviews.tsx) that land here too.
 */
export function ReviewTab({ params }: { params: string[] }) {
  const route = parseReviewRoute(params);
  return route.id ? <ReviewDetail key={route.id} id={route.id} /> : <ReviewHome />;
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Icon, Panel, SegmentedControl } from '@server-guardian/ui';
import { AuthRequiredError, fetchFlows, fetchPersonas, fetchTeams } from '../api';
import { Card } from '../components/Card';
import { Disclosure } from '../components/Disclosure';
import { CheckboxField } from '../components/CheckboxField';
import { Notice } from '../components/Notice';
import { ProjectSwitcher } from '../components/ProjectSwitcher';
import { Row, Stack, muted } from '../components/layout';
import type { DebateJobState } from '../hooks/useDebateJob';
import { useHubData } from '../hooks/useHubData';
import { useNarrow } from '../hooks/useNarrow';
import { useRunRounds } from '../hooks/useRunRounds';
import { sortRuns } from '../lib/agentMonitor';
import { visibleJobs } from '../lib/debate';
import { errorText } from '../lib/format';
import { isWikiJob } from '../lib/wiki';
import { hrefFor, navigate } from '../router';
import { useApprovalsStore } from '../state/approvalsStore';
import { useDebateStore } from '../state/debateStore';
import { hasNoRepository } from '../lib/project';
import { useProjectStore } from '../state/projectStore';
import type { DebateJob, TeamCatalog } from '../types';
import { RunDetail } from './agents/RunDetail';
import { RunList } from './agents/RunList';
import { AskTab } from './ask/AskTab';
import { JobResult } from './debate/JobResult';
import { StartForm } from './debate/StartForm';
import { ArtifactsTab } from './artifacts/ArtifactsTab';
import { ResidentsTab } from './residents/ResidentsTab';
import { ReviewTab } from './review/ReviewTab';
import { ResultsTab } from './results/ResultsTab';
import { WikiTab } from './wiki/WikiTab';
import type { ContinueRequest, RerunInputs } from './debate/StartForm';
import { tr } from '../i18n';

/** Limits used by the form when the team list could not be loaded (the hub still validates). */
const FALLBACK_CATALOG: TeamCatalog = { providers: [], installed: [], tools: [], max_critics: 4, max_rounds: 10 };

const LIST_POLL_MS = 5000;

/**
 * The selected debate: the live monitor (lanes, outcome, timeline) while it runs and the result (verdict,
 * approvals, Publish) once it has finished. The watched job lives in the app-level debate store, which
 * keeps polling it, so the view switches on its own when the debate ends.
 */
function DebateDetail({
  id,
  listJob,
  watcher,
  onListChanged,
  onContinue,
  onRerun,
  onActionsChanged,
}: {
  id: string;
  listJob: DebateJob | null;
  watcher: DebateJobState;
  onListChanged: () => void;
  onContinue: (thread: string) => void;
  onRerun: (inputs: RerunInputs) => void;
  onActionsChanged: () => void;
}) {
  const [openError, setOpenError] = useState('');
  const [activityOpen, setActivityOpen] = useState(false);
  // A code wiki build has no result view: the monitor is its whole detail, and the debate watcher is not used.
  const wikiListed = isWikiJob(listJob);
  // Load the selected job into the watcher unless it is already the watched one.
  const shownId = useRef<string | null>(null);
  shownId.current = watcher.job?.id ?? null;
  const openJob = watcher.open;
  useEffect(() => {
    setOpenError('');
    if (wikiListed || shownId.current === id) return;
    let current = true;
    openJob(id).catch((err) => {
      if (current && !(err instanceof AuthRequiredError)) setOpenError(tr.debate.page.openFailed(id, errorText(err)));
    });
    return () => {
      current = false;
    };
  }, [id, openJob, wikiListed]);

  const watched = watcher.job?.id === id ? watcher.job : null;
  const status = watched?.status ?? listJob?.status;

  if (wikiListed || isWikiJob(watched)) {
    return <RunDetail key={id} id={id} listJob={listJob ?? watched} onChanged={onListChanged} />;
  }
  if (status === 'running') {
    return <RunDetail key={id} id={id} listJob={listJob ?? watched} onChanged={onListChanged} />;
  }
  if (watched) {
    return (
      <Stack gap="var(--sg-s-6)">
        <JobResult
          job={{ ...watched, thread_published: watched.thread_published ?? listJob?.thread_published }}
          error={watcher.error}
          cancelling={watcher.cancelling}
          onCancel={() => void watcher.cancel()}
          onContinue={onContinue}
          onRerun={onRerun}
          onOpenAction={(actionId) => navigate('approvals', [String(actionId)])}
          onActionsChanged={onActionsChanged}
          onThreadChanged={() => {
            onListChanged();
            void openJob(id).catch(() => undefined);
          }}
        />
        {/* The recorded run stays reachable after the debate ended; it is only fetched once opened. */}
        <Disclosure summary={tr.debate.page.activity} open={activityOpen} onToggle={setActivityOpen}>
          {activityOpen ? <RunDetail key={id} id={id} listJob={listJob ?? watched} onChanged={onListChanged} /> : null}
        </Disclosure>
      </Stack>
    );
  }
  if (openError) return <Notice tone="crit">{openError}</Notice>;
  return <Card style={{ textAlign: 'center', ...muted }}>{tr.debate.page.loadingDebate}</Card>;
}

/**
 * Debates: one list of the selected project's debates (closed threads hidden unless asked for) with the
 * selected debate beside it, and a collapsible "New debate" form on top. `jobId` is the
 * `#/debates/:jobId` param; without it the newest running (else newest) debate is shown.
 * Projects, personas and teams are edited in Settings; proposed actions are decided in Approvals.
 */
function DebatesView({ jobId }: { jobId?: string }) {
  const teams = useHubData(fetchTeams);
  // An older hub has no such endpoint: the error leaves the list empty and the picker hidden.
  const flows = useHubData(fetchFlows);
  const personas = useHubData(fetchPersonas);
  const { selected } = useProjectStore();
  const { watcher, recent } = useDebateStore();
  const { refresh } = useApprovalsStore();
  const [showClosed, setShowClosed] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [continueRequest, setContinueRequest] = useState<ContinueRequest | null>(null);

  // One list for the page and Overview: the store's `recent` (polled by DebatesPage while it is open).
  const list = recent;
  const reload = list.reload;

  const onStarted = useCallback(() => {
    reload();
    setFormOpen(false);
  }, [reload]);
  const openForm = useCallback(() => setFormOpen(true), []);

  const all = list.data ?? [];
  const jobs = visibleJobs(all, showClosed);
  const hidden = all.length - jobs.length;
  // Narrow screens show the list or the selected debate, never both; wide screens preselect the newest.
  const narrow = useNarrow();
  const selectedId = jobId || (narrow ? null : sortRuns(jobs)[0]?.id) || null;
  const showList = !narrow || !jobId;
  const showDetail = !narrow || !!jobId;
  const rounds = useRunRounds(jobs.filter((j) => j.status === 'running').map((j) => j.id), selectedId);
  const listJob = all.find((j) => j.id === selectedId) ?? null;
  const shown = { ...list, data: list.data ? jobs : null };

  return (
    <Stack>
      {narrow && jobId ? (
        <div>
          <a href={hrefFor('debates')} aria-label={tr.debate.page.backToList}>
            ← {tr.debate.page.backToList}
          </a>
        </div>
      ) : null}
      <div hidden={!showList}>
      <Card>
        <Row style={{ alignItems: 'flex-end', justifyContent: 'space-between' }}>
          <ProjectSwitcher />
          <Row>
            <Button
              variant={formOpen ? 'outline' : 'primary'}
              icon={<Icon name="chat" size={14} />}
              aria-expanded={formOpen}
              onClick={() => setFormOpen((open) => !open)}
            >
              {tr.debate.page.newDebate}
            </Button>
          </Row>
        </Row>
      </Card>

      {/* Always mounted so a draft survives collapsing; hidden rather than removed. */}
      <div hidden={!formOpen}>
        <Panel title={tr.debate.page.newDebate}>
          <StartForm
            project={selected}
            flows={flows.data?.flows}
            personas={personas.data?.personas ?? []}
            maxRounds={(teams.data?.catalog ?? FALLBACK_CATALOG).max_rounds || 10}
            watcher={watcher}
            continueRequest={continueRequest}
            onStarted={onStarted}
            onPrefill={openForm}
          />
        </Panel>
      </div>

      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-8)', alignItems: 'flex-start' }}>
        {showList ? <div style={{ flex: '1 1 300px', minWidth: 0 }}>
          <Panel
            title={list.data ? tr.debate.page.listTitleCount(jobs.length) : tr.debate.page.listTitle}
            actions={
              <Button size="sm" variant="outline" loading={list.loading} onClick={list.reload}>
                {tr.common.refresh}
              </Button>
            }
          >
            <Stack gap="var(--sg-s-5)">
              <CheckboxField checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)}>
                {tr.debate.page.showClosed}
              </CheckboxField>
              <RunList state={shown} selectedId={selectedId} rounds={rounds} />
              {hidden > 0 ? <div style={{ ...muted, fontSize: 12 }}>{tr.debate.page.closedHidden(hidden)}</div> : null}
            </Stack>
          </Panel>
        </div> : null}
        {showDetail ? <div style={{ flex: '3 1 440px', minWidth: 0 }}>
          {selectedId ? (
            <DebateDetail
              key={selectedId}
              id={selectedId}
              listJob={listJob}
              watcher={watcher}
              onListChanged={() => {
                reload();
              }}
              onContinue={(thread) => setContinueRequest((prev) => ({ thread, n: (prev?.n ?? 0) + 1 }))}
              onRerun={(rerun) => setContinueRequest((prev) => ({ rerun, n: (prev?.n ?? 0) + 1 }))}
              onActionsChanged={refresh}
            />
          ) : (
            <Card style={{ textAlign: 'center', ...muted }}>
              {list.data ? tr.debate.page.empty : tr.debate.page.loadingList}
            </Card>
          )}
        </div> : null}
      </div>
    </Stack>
  );
}

/**
 * Debates, the project's code wiki and its results. `#/debates[/:jobId]` is the debate list with the selected run;
 * `#/debates/wiki[/:page]` is the Wiki view, `#/debates/results[/:thread]` the Results view, `#/debates/ask[/:id]`
 * the Quick ask view (solo runs, which are never listed with the debates) and `#/debates/residents[?agent=]` the
 * resident agents monitor (read-only, hub-wide). Code wiki build jobs are listed with the debates (marked as such)
 * because the monitor beside the list follows them too.
 */
export function DebatesPage({ jobId, wikiPage, extra = [], query = {} }: { jobId?: string; wikiPage?: string; extra?: string[]; query?: Record<string, string> }) {
  const { recent } = useDebateStore();
  const reload = recent.reload;
  useEffect(() => {
    const timer = window.setInterval(reload, LIST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  // An ops or design project has no repository, so no code wiki. A design project instead has an Artifacts tab
  // (`#/debates/artifacts/...`): the documents and diagrams its debates wrote. Other kinds reach the viewer by link only.
  const { selected, projects } = useProjectStore();
  const project = projects.data?.projects.find((p) => p.id === selected);
  const noRepo = hasNoRepository(project);
  const design = project?.data.kind === 'design';
  const wiki = jobId === 'wiki' && !noRepo;
  // The Artifacts viewer is open to every kind: a code or ops report kept as a document lands there too.
  const artifacts = jobId === 'artifacts';
  const results = jobId === 'results';
  // Quick ask (solo agent) belongs to every project kind too: a question a single role answers alone.
  const ask = jobId === 'ask';
  // Resident agents (mail store monitor) are hub-wide, so every project kind sees the same tab.
  const residents = jobId === 'residents';
  // Quick review (docs/design/verification-layer.md, V1c): one reviewer checks one change; code projects only.
  const review = jobId === 'review' && !noRepo;
  const resultsTab = { value: 'results', label: tr.debate.page.tabResults };
  const debatesTab = { value: 'debates', label: tr.debate.page.tabDebates };
  const askTab = { value: 'ask', label: tr.debate.page.tabAsk };
  const residentsTab = { value: 'residents', label: tr.debate.page.tabResidents };
  const reviewTab = { value: 'review', label: tr.debate.page.tabReview };
  // Every project kind has a Results tab: the debate reports it produced (and, for design, its artifacts).
  // Order of docs/design/ui-verification-review.md: the quick review first for code projects, resident agents last
  // (they are not tied to the selected project).
  const tabs = noRepo
    ? design
      ? [debatesTab, askTab, { value: 'artifacts', label: tr.debate.page.tabArtifacts }, resultsTab, residentsTab]
      : [debatesTab, askTab, resultsTab, residentsTab]
    : [reviewTab, debatesTab, askTab, { value: 'wiki', label: tr.debate.page.tabWiki }, resultsTab, residentsTab];
  return (
    <Stack>
      <SegmentedControl
        aria-label={tr.debate.page.sections}
        options={tabs}
        value={review ? 'review' : wiki ? 'wiki' : artifacts ? (design ? 'artifacts' : 'results') : results ? 'results' : ask ? 'ask' : residents ? 'residents' : 'debates'}
        onChange={(v) => navigate('debates', v === 'debates' ? [] : [v])}
      />
      {review ? (
        <ReviewTab params={[wikiPage, ...extra].filter((p): p is string => p !== undefined)} />
      ) : wiki ? (
        <WikiTab page={wikiPage} />
      ) : artifacts ? (
        <ArtifactsTab params={[wikiPage, ...extra].filter((p): p is string => p !== undefined)} />
      ) : results ? (
        <ResultsTab params={[wikiPage, ...extra].filter((p): p is string => p !== undefined)} />
      ) : ask ? (
        <AskTab params={[wikiPage, ...extra].filter((p): p is string => p !== undefined)} />
      ) : residents ? (
        <ResidentsTab query={query} />
      ) : (
        <DebatesView
          jobId={
            jobId === 'wiki' || jobId === 'artifacts' || jobId === 'results' || jobId === 'ask' || jobId === 'residents' || jobId === 'review' ? undefined : jobId
          }
        />
      )}
    </Stack>
  );
}

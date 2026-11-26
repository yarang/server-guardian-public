import { useState } from 'react';
import { Button, Icon, Panel, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, fetchPersonas, fetchSoloList, fetchTeams, startSolo } from '../../api';
import { Ago } from '../../components/Ago';
import { Card } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { DataGate } from '../../components/DataGate';
import { Disclosure } from '../../components/Disclosure';
import { Notice } from '../../components/Notice';
import { ProjectSwitcher } from '../../components/ProjectSwitcher';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { TASK_MAX, jobStatusTone, parseTime } from '../../lib/debate';
import { errorText } from '../../lib/format';
import { hasNoRepository } from '../../lib/project';
import { SOLO_LIST_POLL_MS, askParams, isGeneral, parseAskRoute, soloProviders, soloRoles, splitPaths } from '../../lib/solo';
import { hrefFor, navigate } from '../../router';
import { useProjectStore } from '../../state/projectStore';
import type { SoloListItem, SoloStart } from '../../types';
import { tr } from '../../i18n';
import { AskDetail } from './AskDetail';

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

/** What a list row shows of a question: its first line. */
const firstLine = (text: string | undefined): string => (text ?? '').split('\n')[0].trim();

/** One recent solo run: its question's first line, status, role, citation summary and age. */
function SoloRow({ item }: { item: SoloListItem }) {
  const created = item.created ? parseTime(item.created) : null;
  return (
    <a
      href={hrefFor('debates', askParams({ id: item.id }))}
      aria-label={tr.ask.openRun(firstLine(item.question) || item.id)}
      style={rowStyle}
    >
      <span
        style={{ display: '-webkit-box', WebkitLineClamp: 1, WebkitBoxOrient: 'vertical', overflow: 'hidden', overflowWrap: 'anywhere' }}
        title={item.question}
      >
        {firstLine(item.question) || item.id}
      </span>
      <span style={metaStyle}>
        <StatusChip tone={jobStatusTone(item.status)} title={item.status}>
          {tr.agents.jobStatus[item.status] ?? item.status}
        </StatusChip>
        {isGeneral(item) ? <span style={{ ...muted, fontSize: 12 }}>{tr.ask.generalLabel}</span> : null}
        {item.role ? <span style={{ ...mono, ...muted, fontSize: 12 }}>{item.role}</span> : null}
        {item.summary ? <span style={{ ...muted, fontSize: 12 }}>{item.summary}</span> : null}
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
 * The Quick ask form: one question to one role of the selected project, or — with the general toggle on —
 * one question answered from general knowledge alone, with no project. A 202 hands the run to the detail
 * view, which follows it to its answer; the hub's 400/429 message stays here in the form.
 */
function AskForm({ project, noRepo }: { project: string; noRepo: boolean }) {
  const personas = useHubData(fetchPersonas);
  // The hub's installed providers; without them (older hub, failed request) the form falls back to a fixed list.
  const teams = useHubData(fetchTeams);
  const [question, setQuestion] = useState('');
  const [role, setRole] = useState('');
  const [provider, setProvider] = useState('');
  const [general, setGeneral] = useState(false);
  const [paths, setPaths] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);

  const onAsk = async () => {
    const text = question.trim();
    if (!text || starting) return;
    setStarting(true);
    setStartError('');
    // A general question carries no project and no context: the hub refuses both with general set.
    const req: SoloStart = general ? { question: text, general: true } : { question: text, project };
    if (role) req.role = role;
    if (provider) req.provider = provider;
    if (!general) {
      const contextPaths = noRepo ? [] : splitPaths(paths);
      if (contextPaths.length) req.context_paths = contextPaths;
    }
    try {
      const started = await startSolo(req);
      navigate('debates', askParams({ id: started.id }));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setStartError(errorText(err));
    } finally {
      setStarting(false);
    }
  };

  return (
    <Stack gap="var(--sg-s-6)">
      <TextAreaField
        label={tr.ask.question}
        rows={4}
        maxLength={TASK_MAX}
        placeholder={tr.ask.questionPlaceholder}
        hint={`${question.length} / ${TASK_MAX}`}
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
      />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)', alignItems: 'flex-start' }}>
        <SelectField
          label={tr.ask.role}
          value={role}
          onChange={(e) => setRole(e.target.value)}
          options={[
            { value: '', label: tr.ask.roleDefault },
            // An older hub has no personas endpoint: the default role is all the form offers.
            ...soloRoles(personas.data?.personas ?? []).map((p) => ({ value: p.id, label: `${p.name || p.id} (${p.id})` })),
          ]}
        />
        <SelectField
          label={tr.ask.provider}
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          options={[
            { value: '', label: tr.ask.providerDefault },
            ...soloProviders(teams.data?.catalog?.installed, general).map((p) => ({ value: p, label: p })),
          ]}
        />
      </div>
      <CheckboxField
        checked={general}
        onChange={(e) => {
          const on = e.target.checked;
          setGeneral(on);
          // A provider the hub refuses for a general question must not stay selected behind the toggle.
          if (on && !soloProviders(teams.data?.catalog?.installed, true).includes(provider)) setProvider('');
        }}
      >
        {tr.ask.general}
      </CheckboxField>
      {general ? <Notice tone="info">{tr.ask.generalHint}</Notice> : null}
      {general || noRepo ? null : (
        <Disclosure summary={tr.ask.advanced} open={advancedOpen} onToggle={setAdvancedOpen}>
          <TextAreaField
            label={tr.debate.start.refCode}
            mono
            rows={3}
            placeholder="apps/agent/*.go"
            hint={tr.debate.start.refCodeHint}
            value={paths}
            onChange={(e) => setPaths(e.target.value)}
          />
        </Disclosure>
      )}
      <div>
        <Button
          variant="primary"
          icon={<Icon name="chat" size={14} />}
          loading={starting}
          disabled={!question.trim() || starting}
          onClick={() => void onAsk()}
        >
          {starting ? tr.ask.starting : tr.ask.ask}
        </Button>
      </div>
      {startError ? <Notice tone="crit">{startError}</Notice> : null}
    </Stack>
  );
}

/** The Quick ask home: the form (the project is chosen above it) and the recent solo runs below it. */
function AskHome() {
  const { selected, projects } = useProjectStore();
  // An ops or design project reads no code, so the reference-code field is hidden (as the debate form does).
  const noRepo = hasNoRepository(projects.data?.projects.find((p) => p.id === selected));
  const recent = useHubData(fetchSoloList, { pollMs: SOLO_LIST_POLL_MS });
  return (
    <Stack>
      <ProjectSwitcher />
      <Card>
        <AskForm project={selected} noRepo={noRepo} />
      </Card>
      <Panel title={tr.ask.recentTitle}>
        <DataGate state={recent} what={tr.ask.listWhat}>
          {(runs) =>
            runs.length === 0 ? (
              <div style={{ ...muted, fontSize: 13 }}>{tr.ask.empty}</div>
            ) : (
              <nav aria-label={tr.ask.listAria} style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
                {runs.map((run) => (
                  <SoloRow key={run.id} item={run} />
                ))}
              </nav>
            )
          }
        </DataGate>
      </Panel>
    </Stack>
  );
}

/**
 * Quick ask (solo agent, docs/design/solo-agent.md): one role answers one question alone — no Judge, no
 * round, read-only, labelled SOLO. `#/debates/ask` is the form and the recent runs; `#/debates/ask/<id>`
 * follows one run to its answer.
 */
export function AskTab({ params }: { params: string[] }) {
  const route = parseAskRoute(params);
  return route.id ? <AskDetail key={route.id} id={route.id} /> : <AskHome />;
}

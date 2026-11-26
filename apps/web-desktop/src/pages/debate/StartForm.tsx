import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Icon, TextField } from '@server-guardian/ui';
import { AuthRequiredError, startDebate } from '../../api';
import { Disclosure } from '../../components/Disclosure';
import { Notice } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Stack } from '../../components/layout';
import type { DebateJobState } from '../../hooks/useDebateJob';
import { TASK_MAX, THREAD_ID_RE, checkGlobs, parseGlobLines } from '../../lib/debate';
import { errorText } from '../../lib/format';
import { navigate } from '../../router';
import { hasNoRepository, opsProjectFor } from '../../lib/project';
import { clearDebatePrefill, useDebatePrefill } from '../../state/debatePrefill';
import { useProjectStore } from '../../state/projectStore';
import { buildParams, paramDefaults, paramLabel, paramMax } from '../../lib/flowTemplates';
import type { ParamIssue } from '../../lib/flowTemplates';
import { CLOSE, characterFits, startableFlows } from '../../lib/flowGraph';
import type { DebateStartRequest, FlowEntry, PersonaConfig } from '../../types';
import { tr } from '../../i18n';

/** The inputs of a finished debate, handed to the form by "Run again" (a fresh run: no thread is carried over). */
export interface RerunInputs {
  project?: string;
  task: string;
  /** The team or flow the debate ran with; run again picks the flow of that id when the list has it. */
  team: string;
  rounds: number;
  contextPaths: string[];
  contextRef: string;
}

/**
 * A request to continue a thread (`thread`) or to run a finished debate again (`rerun`) from the result view;
 * `n` changes on every request so repeats still apply.
 */
export interface ContinueRequest {
  thread?: string;
  rerun?: RerunInputs;
  n: number;
}

/** The sentence for a parameter problem, naming the field by the label the form shows. */
function issueText(issue: ParamIssue, flow: FlowEntry): string {
  const spec = flow.data.params?.[issue.name];
  const name = spec ? paramLabel(issue.name, spec) : issue.name;
  const t = tr.debate.start.template;
  switch (issue.code) {
    case 'required': return t.required(name);
    case 'tooLong': return t.tooLong(name, issue.max);
    case 'oneLine': return t.oneLine(name);
    case 'notInt': return t.notInt(name);
    case 'range': return t.range(name, issue.min, issue.max);
    case 'notOption': return t.notOption(name);
  }
}

/** The characters of a flow's seats, each swappable for another that can take the seat's job (D7). */
function SeatSwaps({ flow, personas, swaps, onChange }: {
  flow: FlowEntry;
  personas: PersonaConfig[];
  swaps: Record<string, string>;
  onChange: (swaps: Record<string, string>) => void;
}) {
  const name = (id: string) => personas.find((p) => p.id === id)?.name || id;
  const seats = [
    ...flow.data.seats.filter((s) => s.job !== 'summarise' && s.character).map((s) => ({ id: s.id, job: s.job, mode: s.mode, character: s.character as string })),
    { id: CLOSE, job: 'close' as const, mode: undefined, character: flow.data.close.character },
  ];
  return (
    <Disclosure summary={tr.flows.start.seats}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 'var(--sg-s-5)' }}>
        {seats.map((seat) => (
          <SelectField
            key={seat.id}
            label={seat.id === CLOSE ? tr.flows.fixed.close : `${seat.id} · ${tr.flows.job[seat.job] ?? seat.job}`}
            value={swaps[seat.id] ?? ''}
            onChange={(e) => {
              const next = { ...swaps };
              if (e.target.value) next[seat.id] = e.target.value;
              else delete next[seat.id];
              onChange(next);
            }}
            options={[
              { value: '', label: tr.flows.start.seatDefault(name(seat.character)) },
              ...personas
                .filter((p) => p.id !== seat.character && characterFits(p, seat.job, seat.mode))
                .map((p) => ({ value: p.id, label: `${p.name || p.id} (${p.id})` })),
            ]}
          />
        ))}
      </div>
    </Disclosure>
  );
}

/**
 * The "New debate" form: flow, task and the advanced fields (rounds, continue a thread, reference code). Without a
 * chosen flow the debate runs the project's default flow (its `team` key, D7 of docs/design/flow-graph.md).
 * It stays mounted while collapsed so a draft survives. The watcher is owned by the app-level debate
 * store: starting a job hands it to the watcher and moves the URL to `#/debates/<id>`.
 */
export function StartForm({
  project,
  flows,
  personas = [],
  maxRounds,
  watcher,
  continueRequest,
  onStarted,
  onPrefill,
}: {
  /** Project the debate reads and changes (configs/projects); chosen in the project switcher. */
  project: string;
  /** Flows of the hub (stored, templates and teams converted); empty on an older hub, then the picker is not shown. */
  flows?: FlowEntry[];
  /** Characters a flow's seats may be swapped to. */
  personas?: PersonaConfig[];
  maxRounds: number;
  watcher: DebateJobState;
  /** Set by the result view's "Continue thread" action. */
  continueRequest: ContinueRequest | null;
  /** Called after the hub accepted the job (reload the list, collapse the form). */
  onStarted?: () => void;
  /** Called when a task or a thread was handed in, so the page can expand the form. */
  onPrefill?: () => void;
}) {
  // A flow seats its own characters: its parameter values (and, without its own text, the task) are asked instead.
  const [templateId, setTemplateId] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [swaps, setSwaps] = useState<Record<string, string>>({});
  const [task, setTask] = useState('');
  const [thread, setThread] = useState('');
  const [rounds, setRounds] = useState('');
  // Set once the user edits the reference-code fields (or continues a thread).
  const [ctx, setCtx] = useState<{ paths: string; ref: string } | null>(null);
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);
  const startingRef = useRef(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const { projects, select } = useProjectStore();
  // An ops or design project reads no code: the reference-code fields are hidden and never sent.
  const projectEntry = projects.data?.projects.find((p) => p.id === project);
  const noRepo = hasNoRepository(projectEntry);
  const startable = useMemo(() => startableFlows(flows, projectEntry?.data.kind), [flows, projectEntry]);
  // A flow that stops being startable (another project, a changed list) falls back to the free task.
  const template = startable.find((t) => t.id === templateId);
  const flowTask = !!template?.data.task?.trim();

  // Hand-off from another page (Overview, node, group, SSH audit): take the prefilled task once, then clear it.
  // Fleet, node and group debates belong to an ops project; the page switches to it so the debate lands there.
  const prefill = useDebatePrefill();
  const projectList = projects.data?.projects;
  const listPending = !projectList && projects.error === null;
  useEffect(() => {
    // A scoped hand-off waits for the project list (or its failure) so it can pick the ops project.
    if (prefill && !(prefill.ops && listPending)) {
      setTask(prefill.task);
      setTemplateId('');
      const target = prefill.ops ? opsProjectFor(projectList, prefill.ops) : undefined;
      if (target) select(target);
      clearDebatePrefill();
      onPrefill?.();
    }
  }, [prefill, onPrefill, select, projectList, listPending]);

  // Continue a thread: fill the thread id and leave reference code empty so the thread's own code and commit are reused.
  useEffect(() => {
    if (!continueRequest) return;
    const again = continueRequest.rerun;
    if (again) {
      // Run again: the same inputs in a new thread. A job of another project is run in that project.
      if (again.project) select(again.project);
      setTask(again.task);
      // The lineup it ran with: the flow of that id (teams are listed converted), unless it writes its own task.
      const same = startable.find((t) => t.id === again.team && !t.data.task?.trim());
      setTemplateId(same ? same.id : '');
      setValues(same ? paramDefaults(same.data) : {});
      setSwaps({});
      setRounds(again.rounds ? String(again.rounds) : '');
      setThread('');
      setCtx({ paths: again.contextPaths.join('\n'), ref: again.contextRef });
      setAdvancedOpen(true);
      onPrefill?.();
      return;
    }
    setThread(continueRequest.thread ?? '');
    setTemplateId('');
    setAdvancedOpen(true);
    setCtx({ paths: '', ref: '' });
    onPrefill?.();
    document.getElementById('debate-thread')?.focus();
    // `select` is left out of the deps on purpose: only a new request may re-run this.
  }, [continueRequest, onPrefill]);

  const shown = ctx ?? { paths: '', ref: '' };
  const ctxHint = thread && !shown.paths.trim() ? tr.debate.start.reuseThread : undefined;
  const projectFlow = projectEntry?.data.team;

  /** Choosing a flow (or the free task) resets the parameter values and the swapped characters. */
  const pickTemplate = (id: string) => {
    setTemplateId(id);
    const entry = startable.find((t) => t.id === id);
    setValues(entry ? paramDefaults(entry.data) : {});
    setSwaps({});
    setStartError('');
  };
  const setValue = (name: string, value: string) => setValues((v) => ({ ...v, [name]: value }));

  const onStart = async () => {
    if (startingRef.current) return;
    const text = task.trim();
    let params: Record<string, string> | undefined;
    if (template) {
      const built = buildParams(template.data, values);
      if ('issue' in built) return setStartError(issueText(built.issue, template));
      params = built.params;
    }
    if (!flowTask) {
      if (!text) return setStartError(tr.debate.start.enterTask);
      if (text.length > TASK_MAX) return setStartError(tr.debate.start.taskTooLong(TASK_MAX));
    }
    /** Reports a problem with a field inside Advanced and opens it so the field is in view. */
    const advancedError = (message: string) => {
      setAdvancedOpen(true);
      setStartError(message);
    };
    const req: DebateStartRequest = template ? { flow: template.id, project } : { task: text, project };
    if (template && params && Object.keys(params).length) req.params = params;
    if (template && !flowTask) req.task = text;
    if (template && Object.keys(swaps).length) req.seats = swaps;
    // A template always starts a new thread.
    const threadId = template ? '' : thread.trim();
    if (threadId) {
      if (!THREAD_ID_RE.test(threadId)) return advancedError(tr.debate.start.badThread);
      req.thread_id = threadId;
    }
    if (rounds.trim()) {
      const n = Number(rounds);
      if (!Number.isInteger(n) || n < 1 || n > maxRounds) return advancedError(tr.debate.start.badRounds(maxRounds));
      req.rounds = n;
    }
    if (!noRepo) {
      const paths = parseGlobLines(shown.paths);
      const globError = checkGlobs(paths);
      if (globError) return advancedError(globError);
      if (paths.length) req.context_paths = paths;
      if (shown.ref.trim()) req.context_ref = shown.ref.trim();
    }

    startingRef.current = true;
    setStarting(true);
    setStartError('');
    try {
      const job = await startDebate(req);
      watcher.begin(job);
      onStarted?.();
      navigate('debates', [job.id]);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setStartError(errorText(err));
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
  };

  return (
      <div>
        <Stack gap="var(--sg-s-6)">
          {startable.length ? (
            <SelectField
              label={tr.flows.start.label}
              value={template ? template.id : ''}
              onChange={(e) => pickTemplate(e.target.value)}
              options={[
                { value: '', label: tr.flows.start.free },
                ...startable.map((t) => ({ value: t.id, label: tr.flows.start.use(t.name || t.id) })),
              ]}
            />
          ) : null}
          {template ? (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 'var(--sg-s-6)' }}>
              {Object.entries(template.data.params ?? {}).map(([name, spec]) => {
                const label = paramLabel(name, spec);
                const value = values[name] ?? '';
                if (spec.type === 'enum') {
                  return (
                    <SelectField
                      key={name}
                      label={label}
                      value={value}
                      onChange={(e) => setValue(name, e.target.value)}
                      options={[{ value: '', label: '' }, ...(spec.values ?? []).map((v) => ({ value: v, label: v }))]}
                    />
                  );
                }
                if (spec.type === 'int') {
                  return (
                    <TextField
                      key={name}
                      label={label}
                      type="number"
                      min={spec.min}
                      max={spec.max}
                      step={1}
                      value={value}
                      onChange={(e) => setValue(name, e.target.value)}
                    />
                  );
                }
                const max = paramMax(spec);
                if (spec.type === 'text') {
                  return (
                    <TextAreaField
                      key={name}
                      label={label}
                      rows={4}
                      maxLength={max}
                      hint={max ? `${value.length} / ${max}` : undefined}
                      value={value}
                      onChange={(e) => setValue(name, e.target.value)}
                    />
                  );
                }
                return <TextField key={name} label={label} maxLength={max} value={value} onChange={(e) => setValue(name, e.target.value)} />;
              })}
            </div>
          ) : null}
          {flowTask ? null : (
            <TextAreaField
              label={tr.debate.start.task}
              rows={4}
              maxLength={TASK_MAX}
              placeholder={tr.debate.start.taskPlaceholder}
              hint={`${task.length} / ${TASK_MAX}`}
              value={task}
              onChange={(e) => setTask(e.target.value)}
            />
          )}
          {template ? (
            <SeatSwaps flow={template} personas={personas} swaps={swaps} onChange={setSwaps} />
          ) : (
            <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>
              {projectFlow ? tr.debate.start.projectFlow(projectFlow) : tr.debate.start.projectFlowNone}
            </div>
          )}
          <Disclosure
            summary={tr.debate.start.advanced}
            open={advancedOpen}
            onToggle={setAdvancedOpen}
          >
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)', alignItems: 'flex-start' }}>
              <TextField
                label={tr.debate.start.rounds}
                type="number"
                min={1}
                max={maxRounds}
                step={1}
                placeholder={tr.debate.start.roundsPlaceholder}
                value={rounds}
                onChange={(e) => setRounds(e.target.value)}
              />
              {template ? null : (
                <TextField
                  id="debate-thread"
                  label={tr.debate.start.continueThread}
                  mono
                  placeholder="thread_..."
                  value={thread}
                  onChange={(e) => setThread(e.target.value)}
                />
              )}
            </div>
            {noRepo ? null : <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 'var(--sg-s-6)' }}>
              <TextAreaField
                label={tr.debate.start.refCode}
                mono
                rows={3}
                placeholder="apps/agent/*.go"
                hint={ctxHint ?? tr.debate.start.refCodeHint}
                value={shown.paths}
                onChange={(e) => setCtx({ paths: e.target.value, ref: shown.ref })}
              />
              <TextField
                label="Ref"
                mono
                placeholder="HEAD"
                value={shown.ref}
                onChange={(e) => setCtx({ paths: shown.paths, ref: e.target.value })}
              />
            </div>}
          </Disclosure>
          <div>
            <Button variant="primary" icon={<Icon name="chat" size={14} />} loading={starting} onClick={() => void onStart()}>
              {starting ? tr.debate.start.starting : tr.debate.start.start}
            </Button>
          </div>
          {startError ? <Notice tone="crit">{startError}</Notice> : null}
        </Stack>
      </div>
  );
}

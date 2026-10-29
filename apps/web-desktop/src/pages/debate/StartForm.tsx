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
import type { DebateStartRequest, TeamEntry } from '../../types';
import { tr } from '../../i18n';

/** The inputs of a finished debate, handed to the form by "Run again" (a fresh run: no thread is carried over). */
export interface RerunInputs {
  project?: string;
  task: string;
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

/**
 * The "New debate" form: task, team and the advanced fields (rounds, continue a thread, reference code).
 * It stays mounted while collapsed so a draft survives. The watcher is owned by the app-level debate
 * store: starting a job hands it to the watcher and moves the URL to `#/debates/<id>`.
 */
export function StartForm({
  project,
  teams,
  maxRounds,
  watcher,
  continueRequest,
  onStarted,
  onPrefill,
}: {
  /** Project the debate reads and changes (configs/projects); chosen in the project switcher. */
  project: string;
  teams: TeamEntry[];
  maxRounds: number;
  watcher: DebateJobState;
  /** Set by the result view's "Continue thread" action. */
  continueRequest: ContinueRequest | null;
  /** Called after the hub accepted the job (reload the list, collapse the form). */
  onStarted?: () => void;
  /** Called when a task or a thread was handed in, so the page can expand the form. */
  onPrefill?: () => void;
}) {
  const [team, setTeam] = useState('');
  const [task, setTask] = useState('');
  const [thread, setThread] = useState('');
  const [rounds, setRounds] = useState('');
  // Set once the user edits the reference-code fields (or continues a thread); team defaults then no longer apply.
  const [ctx, setCtx] = useState<{ paths: string; ref: string } | null>(null);
  const [startError, setStartError] = useState('');
  const [starting, setStarting] = useState(false);
  const startingRef = useRef(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const { projects, select } = useProjectStore();
  // An ops or design project reads no code: the reference-code fields are hidden and never sent.
  const noRepo = hasNoRepository(projects.data?.projects.find((p) => p.id === project));

  // Hand-off from another page (Fleet summary, node, group, SSH audit): take the prefilled task once, then clear it.
  // Fleet, node and group debates belong to an ops project; the page switches to it so the debate lands there.
  const prefill = useDebatePrefill();
  const projectList = projects.data?.projects;
  const listPending = !projectList && projects.error === null;
  useEffect(() => {
    // A scoped hand-off waits for the project list (or its failure) so it can pick the ops project.
    if (prefill && !(prefill.ops && listPending)) {
      setTask(prefill.task);
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
      setTeam(again.team);
      setRounds(again.rounds ? String(again.rounds) : '');
      setThread('');
      setCtx({ paths: again.contextPaths.join('\n'), ref: again.contextRef });
      setAdvancedOpen(true);
      onPrefill?.();
      return;
    }
    setThread(continueRequest.thread ?? '');
    setAdvancedOpen(true);
    setCtx({ paths: '', ref: '' });
    onPrefill?.();
    document.getElementById('debate-thread')?.focus();
    // `select` is left out of the deps on purpose: only a new request may re-run this.
  }, [continueRequest, onPrefill]);

  const teamDefaults = useMemo(() => {
    const c = teams.find((t) => t.id === team)?.data?.context;
    return c && Array.isArray(c.paths) && c.paths.length ? { paths: c.paths.map(String).join('\n'), ref: c.ref ? String(c.ref) : '' } : null;
  }, [teams, team]);
  const shown = ctx ?? teamDefaults ?? { paths: '', ref: '' };
  const ctxHint = ctx === null && teamDefaults ? tr.debate.start.teamDefault : thread && !shown.paths.trim() ? tr.debate.start.reuseThread : undefined;

  const onStart = async () => {
    if (startingRef.current) return;
    const text = task.trim();
    if (!text) return setStartError(tr.debate.start.enterTask);
    if (text.length > TASK_MAX) return setStartError(tr.debate.start.taskTooLong(TASK_MAX));
    /** Reports a problem with a field inside Advanced and opens it so the field is in view. */
    const advancedError = (message: string) => {
      setAdvancedOpen(true);
      setStartError(message);
    };
    const req: DebateStartRequest = { task: text, project };
    if (team) req.team = team;
    const threadId = thread.trim();
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
          <TextAreaField
            label={tr.debate.start.task}
            rows={4}
            maxLength={TASK_MAX}
            placeholder={tr.debate.start.taskPlaceholder}
            hint={`${task.length} / ${TASK_MAX}`}
            value={task}
            onChange={(e) => setTask(e.target.value)}
          />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)', alignItems: 'flex-start' }}>
            <SelectField
              label={tr.debate.start.team}
              value={team}
              onChange={(e) => setTeam(e.target.value)}
              options={[
                { value: '', label: tr.debate.start.defaultTeam },
                ...teams.map((t) => {
                  const bad = (t.errors || []).length > 0;
                  return { value: t.id, label: `${t.name || t.id} (${t.id})${bad ? tr.debate.start.invalidSuffix : ''}`, disabled: bad };
                }),
              ]}
            />
          </div>
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
              <TextField
                id="debate-thread"
                label={tr.debate.start.continueThread}
                mono
                placeholder="thread_..."
                value={thread}
                onChange={(e) => setThread(e.target.value)}
              />
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

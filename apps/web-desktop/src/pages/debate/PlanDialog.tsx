import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, ConfirmDialog, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, HubError, fetchCommandSets, getPlan, rejectPlan, runPlan } from '../../api';
import { Caption } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { LoadingNote } from '../../components/DataGate';
import { Disclosure } from '../../components/Disclosure';
import { Modal } from '../../components/Modal';
import { MonoBlock } from '../../components/MonoBlock';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { actionStatusTone, failMessage, needsTypedConfirm, planState, planStateTone, riskLabel, riskTone } from '../../lib/debate';
import { errorText } from '../../lib/format';
import type { ActionItem, CommandSet, PlanItem } from '../../types';
import { ActionResult, ProtectionNotes } from './ActionDetails';
import { CommandSetDialog, NEW_COMMAND_SET } from './CommandSetDialog';
import { PlanGate } from './PlanGate';
import { tr } from '../../i18n';

const POLL_MS = 3000;
type Busy = 'run' | 'reject' | null;
type Msg = { tone: 'ok' | 'crit' | 'warn'; text: string };

/** "name" or "name@version" for each requested package, as the step's arguments name them. */
function packagesOf(step: ActionItem): string {
  const list = (step.arguments as { packages?: Array<{ name?: string; version?: string }> } | null | undefined)?.packages;
  if (!Array.isArray(list)) return '';
  return list.map((p) => (p.version ? `${p.name}@${p.version}` : String(p.name))).join(', ');
}

/** An ad-hoc script step, as a draft command set: only the script and the reason are taken from the step. */
function scriptDraft(step: ActionItem): CommandSet | null {
  if (step.tool !== 'ops:run_script') return null;
  const args = (step.arguments ?? {}) as { script?: unknown; reason?: unknown };
  if (typeof args.script !== 'string' || args.script.trim() === '') return null;
  const reason = typeof args.reason === 'string' ? args.reason : step.reason ?? '';
  return { ...NEW_COMMAND_SET, script: args.script, description: reason.slice(0, 300) };
}

function StepDetails({ step }: { step: ActionItem }) {
  const sim = step.preview;
  const draft = scriptDraft(step);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [taken, setTaken] = useState<string[]>([]);
  return (
    <Stack gap="var(--sg-s-3)">
      {draft ? (
        <>
          <Caption>{tr.debate.commandSet.script}</Caption>
          <MonoBlock text={draft.script} maxHeight={200} />
          <Row>
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                fetchCommandSets()
                  .then((list) => setTaken((list.sets ?? []).map((s) => s.id)))
                  .catch(() => setTaken([]))
                  .finally(() => setSaving(true))
              }
            >
              {tr.debate.commandSet.saveAsSet}
            </Button>
            {saved ? <span style={{ ...muted, fontSize: 12 }}>{tr.debate.commandSet.saved(saved)}</span> : null}
          </Row>
          {saving ? (
            <CommandSetDialog
              set={draft}
              isNew
              fromScript
              takenIds={taken}
              onClose={() => setSaving(false)}
              onSaved={(res) => {
                setSaved(res.id);
                setSaving(false);
              }}
              onDeleted={() => setSaving(false)}
            />
          ) : null}
        </>
      ) : null}
      <Caption>{tr.debate.plans.simulation}</Caption>
      {sim?.error ? <Notice tone="warn">{sim.error}</Notice> : null}
      {sim ? <ProtectionNotes preview={sim} /> : null}
      {sim?.text ? <MonoBlock text={sim.text} maxHeight={160} /> : null}
      {!sim ? <div style={{ ...muted, fontSize: 12 }}>{tr.debate.plans.deferredPreview}</div> : null}
      {sim && !sim.error && !sim.text ? <div style={{ ...muted, fontSize: 12 }}>{tr.debate.plans.noPreview}</div> : null}
      {step.result !== null && step.result !== undefined && step.result !== '' ? (
        <>
          <Caption>{tr.debate.plans.result}</Caption>
          <ActionResult result={step.result} />
        </>
      ) : null}
    </Stack>
  );
}

function stepColumns(): Column<ActionItem>[] {
  return [
    { header: tr.debate.plans.colStep, mono: true, render: (s) => s.step ?? `#${s.id}` },
    {
      header: tr.debate.plans.colTool,
      render: (s) => (
        <div style={{ display: 'grid', gap: 4, minWidth: 0 }}>
          <span style={mono}>{s.tool}</span>
          <span style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{packagesOf(s)}</span>
          <Disclosure summary={tr.debate.plans.simulation}>
            <StepDetails step={s} />
          </Disclosure>
        </div>
      ),
    },
    {
      header: tr.debate.plans.colStatus,
      render: (s) => (
        <StatusChip tone={actionStatusTone(s.status)} title={String(s.status)}>
          {tr.debate.actionStatus[String(s.status).toUpperCase()] ?? String(s.status)}
        </StatusChip>
      ),
    },
    {
      header: tr.debate.plans.colRisk,
      render: (s) => (
        <StatusChip tone={riskTone(s.risk)} title={s.risk}>
          {riskLabel(s.risk)}
        </StatusChip>
      ),
    },
    {
      header: tr.debate.plans.colUndo,
      render: (s) => (s.reversible === undefined ? '' : s.reversible ? tr.debate.plans.undoable : tr.debate.plans.irreversible),
    },
    { header: tr.debate.plans.colRequires, mono: true, render: (s) => (s.requires ?? []).join(', ') },
    {
      header: tr.debate.plans.colOnFailure,
      render: (s) => (s.on_failure === 'continue' ? tr.debate.plans.onFailureContinue : tr.debate.plans.onFailureStop),
    },
  ];
}

/**
 * Detail, run and reject of one plan. The hub answers a run with 202 as soon as it is running (a plan can
 * take a long time), so the dialog polls the plan while a step is RUNNING and shows each step as it ends.
 * Run needs a confirmation: a checkbox for low / medium risk, typing the plan id otherwise.
 */
export function PlanDialog({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const [plan, setPlan] = useState<PlanItem | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [msg, setMsg] = useState<Msg | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [starting, setStarting] = useState(false);
  const [confirmRun, setConfirmRun] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [confirmReject, setConfirmReject] = useState(false);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const refresh = useCallback(async (): Promise<PlanItem | null> => {
    try {
      const fresh = await getPlan(id);
      if (alive.current) {
        setPlan(fresh);
        setLoadError(null);
      }
      return fresh;
    } catch (err) {
      if (!alive.current) return null;
      if (err instanceof AuthRequiredError) onClose();
      else setLoadError(tr.debate.plans.loadFailed(id, errorText(err)));
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    setPlan(null);
    void refresh();
  }, [refresh]);

  const state = plan ? planState(plan) : null;
  const running = state === 'running' || starting;

  // Poll while a step runs (or the run was just started): the hub keeps going without this page.
  const wasRunning = useRef(false);
  useEffect(() => {
    if (!running) {
      if (wasRunning.current && plan) {
        wasRunning.current = false;
        setMsg({ tone: state === 'done' ? 'ok' : 'warn', text: tr.debate.plans.finished(tr.debate.plans.state[state ?? 'done'] ?? String(state)) });
        onChanged();
      }
      return;
    }
    wasRunning.current = true;
    const timer = window.setInterval(() => {
      void refresh().then((fresh) => {
        if (fresh && planState(fresh) !== 'running') setStarting(false);
      });
    }, POLL_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, state]);

  const doRun = async () => {
    setConfirmRun(false);
    setReviewed(false);
    setBusy('run');
    setMsg(null);
    try {
      const r = await runPlan(id);
      if (!alive.current) return;
      if (r.started) {
        setStarting(true);
        setMsg({ tone: 'ok', text: tr.debate.plans.started });
        await refresh();
      } else if (r.plan) {
        setPlan(r.plan);
        setMsg({ tone: 'ok', text: tr.debate.plans.finished(tr.debate.plans.state[planState(r.plan)] ?? '') });
      }
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof AuthRequiredError) {
        onClose();
        return;
      }
      setMsg({ tone: 'crit', text: err instanceof HubError && err.status === 409 ? `${errorText(err)}\n\n${tr.debate.plans.notRunnable}` : failMessage(err, 'other') });
      if (err instanceof HubError && (err.status === 409 || err.status === 404)) await refresh();
    } finally {
      if (alive.current) setBusy(null);
    }
    onChanged();
  };

  const doReject = async () => {
    setConfirmReject(false);
    setBusy('reject');
    setMsg(null);
    try {
      const before = plan?.counts.PROPOSED ?? 0;
      const next = await rejectPlan(id);
      if (!alive.current) return;
      setPlan(next);
      setMsg({ tone: 'ok', text: tr.debate.plans.rejected(before + (plan?.counts.HELD ?? 0)) });
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof AuthRequiredError) {
        onClose();
        return;
      }
      setMsg({ tone: 'crit', text: failMessage(err, 'other') });
      if (err instanceof HubError && err.status === 409) await refresh();
    } finally {
      if (alive.current) setBusy(null);
    }
    onChanged();
  };

  const title = tr.debate.plans.dialogTitle(id);
  if (!plan) {
    return (
      <Modal title={title} width={900} onClose={onClose}>
        <Stack gap="var(--sg-s-5)">
          {loadError ? <Notice tone="crit">{loadError}</Notice> : <LoadingNote what={tr.debate.plans.what} />}
          <div>
            <Button variant="ghost" onClick={onClose}>
              {tr.common.close}
            </Button>
          </div>
        </Stack>
      </Modal>
    );
  }

  const stateKey = planState(plan);
  const locked = busy !== null || running;
  const byId = new Map(plan.steps.map((s) => [s.step ?? String(s.id), s]));
  const typed = needsTypedConfirm(plan.risk);
  const waiting = (plan.counts.PROPOSED ?? 0) + (plan.counts.HELD ?? 0);
  const total = plan.steps.length;

  return (
    <>
      <Modal title={title} width={900} onClose={busy !== null ? undefined : onClose}>
        <Stack gap="var(--sg-s-6)">
          <Row>
            <StatusChip tone={planStateTone(stateKey)}>{tr.debate.plans.state[stateKey]}</StatusChip>
            <StatusChip tone={riskTone(plan.risk)} title={plan.risk}>
              {riskLabel(plan.risk)}
            </StatusChip>
            <span style={{ ...mono, ...muted, fontSize: 12 }}>
              {Object.entries(plan.counts)
                .map(([k, n]) => `${tr.debate.actionStatus[k] ?? k} ${n}`)
                .join(' · ')}
            </span>
          </Row>
          <Stack gap="var(--sg-s-2)">
            <Caption>{tr.debate.plans.goal}</Caption>
            <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.5 }}>{plan.goal || tr.debate.plans.noGoal}</div>
          </Stack>
          <div style={{ ...muted, fontSize: 12 }}>{plan.stop_plan ? tr.debate.plans.stopPlanNote : tr.debate.plans.continuePlanNote}</div>

          <PlanGate plan={plan} open={waiting > 0} />

          {plan.hosts.map((h) => {
            const rows = h.steps.map((sid) => byId.get(sid)).filter((s): s is ActionItem => s !== undefined);
            return (
              <Stack key={h.host} gap="var(--sg-s-3)">
                <Caption>{tr.debate.plans.nodeSteps(h.host, rows.length)}</Caption>
                <DataTable label={h.host} columns={stepColumns()} rows={rows} getKey={(s) => String(s.id)} />
              </Stack>
            );
          })}

          {waiting > 0 || running ? (
            <Row>
              <Button variant="primary" loading={busy === 'run' || running} disabled={locked || !plan.runnable} onClick={() => setConfirmRun(true)}>
                {busy === 'run' || running ? tr.debate.plans.running : tr.debate.plans.run}
              </Button>
              <Button variant="outline" loading={busy === 'reject'} disabled={locked || waiting === 0} onClick={() => setConfirmReject(true)}>
                {busy === 'reject' ? tr.debate.plans.rejecting : tr.debate.plans.reject}
              </Button>
              {running ? <span style={{ color: 'var(--sg-text-secondary)' }}>{tr.debate.plans.polling}</span> : null}
            </Row>
          ) : null}
          {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button variant="ghost" disabled={busy !== null} onClick={onClose}>
              {tr.common.close}
            </Button>
          </div>
        </Stack>
      </Modal>

      {confirmRun && typed ? (
        <ConfirmDialog
          risk="HIGH"
          title={tr.debate.plans.runQ(id)}
          confirmText={id}
          confirmLabel={tr.debate.plans.runNow}
          onConfirm={() => void doRun()}
          onCancel={() => setConfirmRun(false)}
        >
          <div style={{ lineHeight: 1.6 }}>{tr.debate.plans.runSummary(total, plan.hosts.length, riskLabel(plan.risk))}</div>
        </ConfirmDialog>
      ) : null}

      {confirmRun && !typed ? (
        <Modal title={tr.debate.plans.runQ(id)} width={640} onClose={() => setConfirmRun(false)}>
          <Stack gap="var(--sg-s-5)">
            <div style={{ lineHeight: 1.6 }}>{tr.debate.plans.runSummary(total, plan.hosts.length, riskLabel(plan.risk))}</div>
            <CheckboxField checked={reviewed} onChange={(e) => setReviewed(e.target.checked)}>
              {tr.debate.plans.reviewed}
            </CheckboxField>
            <Row>
              <Button variant="danger" disabled={!reviewed} onClick={() => void doRun()}>
                {tr.debate.plans.runNow}
              </Button>
              <Button variant="ghost" onClick={() => setConfirmRun(false)}>
                {tr.common.back}
              </Button>
            </Row>
          </Stack>
        </Modal>
      ) : null}

      {confirmReject ? (
        <ConfirmModal
          title={tr.debate.plans.rejectQ(id)}
          message={tr.debate.plans.rejectMsg(waiting)}
          confirmLabel={tr.debate.plans.reject}
          onConfirm={() => void doReject()}
          onCancel={() => setConfirmReject(false)}
        />
      ) : null}
    </>
  );
}

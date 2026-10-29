import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, ConfirmDialog, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, HubError, failStaleAction, getAction, jevRecheck, rejectAction } from '../../api';
import { Caption } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { ConfirmModal } from '../../components/ConfirmModal';
import { LoadingNote } from '../../components/DataGate';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import {
  THREAD_ID_RE,
  actionStatusTone,
  changesWorkspace,
  failMessage,
  fmtTime,
  needsTypedConfirm,
  patchSummary,
  riskLabel,
  riskTone,
} from '../../lib/debate';
import { errorText } from '../../lib/format';
import { useOptionalApprovalsStore } from '../../state/approvalsStore';
import { navigate } from '../../router';
import { useOperationsStore } from '../../state/operationsStore';
import type { ActionItem, JevRecheckResponse } from '../../types';
import { ActionArguments, ActionPreviewView, ActionResult, RecheckSummary } from './ActionDetails';
import { tr } from '../../i18n';

type Busy = 'reject' | 'recheck' | 'fail' | null;
type Msg = { tone: 'ok' | 'crit' | 'warn'; text: string };

/** Wording shared by both confirmation variants. */
function RunSummary({ action }: { action: ActionItem }) {
  const target = (action.arguments as Record<string, unknown> | null | undefined)?.target_host;
  return (
    <Stack gap="var(--sg-s-3)">
      <div style={{ lineHeight: 1.6, overflowWrap: 'anywhere' }}>
        {`${tr.debate.dialog.executeTarget}: `}
        <b style={mono}>{action.tool}</b>
        {` (${tr.debate.dialog.riskLabel}: ${action.risk || tr.common.unknown})`}
        {target !== undefined ? (
          <>
            {` · ${tr.debate.dialog.targetNode}: `}
            <b style={mono}>{String(target)}</b>
          </>
        ) : null}
        {` ${tr.debate.dialog.executeNote}`}
      </div>
      {action.tool === 'jev_mcp:apply_patch' ? (
        <div style={{ lineHeight: 1.6, overflowWrap: 'anywhere' }}>
          <b>{`${patchSummary(action)}.`}</b>
          {` ${tr.debate.dialog.patchNote}`}
        </div>
      ) : null}
    </Stack>
  );
}

function ScrollBox({ children }: { children: ReactNode }) {
  return <div style={{ maxHeight: '38vh', overflow: 'auto', minWidth: 0 }}>{children}</div>;
}

/**
 * Detail, execute and reject of one judge action. Execute needs a confirmation step: low / medium risk
 * a checkbox ("I reviewed the arguments"), anything else typing the action id (design system
 * ConfirmDialog). The run itself lives in the operations store: while it is in flight the controls are locked, but the
 * dialog may be closed and the page left; the outcome is shown when the action is opened again.
 */
export function ActionDialog({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const [action, setAction] = useState<ActionItem | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [msg, setMsg] = useState<Msg | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [recheck, setRecheck] = useState<JevRecheckResponse | null>(null);
  const [ranNow, setRanNow] = useState(false);
  const [confirmRun, setConfirmRun] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [confirmReject, setConfirmReject] = useState(false);
  const [confirmFail, setConfirmFail] = useState(false);
  const ops = useOperationsStore();
  const closedThreads = useOptionalApprovalsStore()?.closedThreads;
  const exec = ops.execs[id];
  const running = exec?.phase === 'running';

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let current = true;
    setAction(null);
    setLoadError(null);
    getAction(id)
      .then((a) => {
        if (current) setAction(a);
      })
      .catch((err) => {
        if (!current) return;
        if (err instanceof AuthRequiredError) onClose();
        else setLoadError(tr.debate.dialog.loadFailed(id, errorText(err)));
      });
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  /** Re-read the action after a 409 / 404 or a recheck so the detail matches the hub. */
  const refresh = useCallback(async () => {
    try {
      const fresh = await getAction(id);
      if (alive.current) setAction(fresh);
    } catch {
      // keep what is shown
    }
  }, [id]);

  const handleAuth = (err: unknown): boolean => {
    if (!(err instanceof AuthRequiredError)) return false;
    setBusy(null);
    onClose();
    return true;
  };

  const doRun = (a: ActionItem) => {
    setConfirmRun(false);
    setReviewed(false);
    setMsg(null);
    ops.startExec(a);
  };

  // Applies a finished run from the store, also one that finished while this dialog was closed.
  const settled = exec && exec.phase !== 'running' ? exec : null;
  const loaded = action !== null;
  useEffect(() => {
    if (!settled || !loaded) return;
    if (settled.phase === 'done') {
      const next = settled.action;
      setAction(next);
      setRanNow(true);
      const st = String(next.status).toUpperCase();
      setMsg(st === 'EXECUTED' ? { tone: 'ok', text: tr.debate.dialog.finished(id, next.status) } : { tone: 'crit', text: tr.debate.dialog.finishedWith(id, next.status) });
    } else {
      setMsg({ tone: 'crit', text: settled.message });
      if (settled.status === 409 || settled.status === 404) void refresh();
    }
    ops.ackExec(id);
    onChanged();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled, loaded]);

  const doReject = async (a: ActionItem) => {
    setConfirmReject(false);
    setBusy('reject');
    setMsg(null);
    try {
      const next = await rejectAction(a.id);
      if (!alive.current) return;
      setAction(next);
      setMsg({ tone: 'ok', text: tr.debate.dialog.rejected(a.id) });
    } catch (err) {
      if (!alive.current || handleAuth(err)) return;
      setMsg({ tone: 'crit', text: failMessage(err, 'other') });
      if (err instanceof HubError && err.status === 409) await refresh();
    } finally {
      if (alive.current) setBusy(null);
    }
    onChanged();
  };

  const doFailStale = async (a: ActionItem) => {
    setConfirmFail(false);
    setBusy('fail');
    setMsg(null);
    try {
      const next = await failStaleAction(a.id);
      if (!alive.current) return;
      setAction(next);
      setMsg({ tone: 'ok', text: tr.debate.dialog.markedFailed(a.id) });
    } catch (err) {
      if (!alive.current || handleAuth(err)) return;
      setMsg({ tone: 'crit', text: failMessage(err, 'other') });
      if (err instanceof HubError && err.status === 409) await refresh();
    } finally {
      if (alive.current) setBusy(null);
    }
    onChanged();
  };

  const doRecheck = async (a: ActionItem) => {
    setBusy('recheck');
    setMsg(null);
    setRecheck(null);
    try {
      const r = await jevRecheck(a.thread_id);
      if (!alive.current) return;
      setRecheck(r);
      await refresh();
    } catch (err) {
      if (!alive.current || handleAuth(err)) return;
      setMsg({ tone: 'crit', text: failMessage(err, 'recheck') });
    } finally {
      if (alive.current) setBusy(null);
    }
    onChanged();
  };

  const title = tr.debate.dialog.title(id);
  if (!action) {
    return (
      <Modal title={title} width={760} onClose={onClose}>
        <Stack gap="var(--sg-s-5)">
          {loadError ? <Notice tone="crit">{loadError}</Notice> : <LoadingNote what={tr.debate.actions.what} />}
          <div>
            <Button variant="ghost" onClick={onClose}>
              {tr.common.close}
            </Button>
          </div>
        </Stack>
      </Modal>
    );
  }

  const a = action;
  const status = String(a.status).toUpperCase();
  const planStep = Boolean(a.plan_id);
  const isProposed = status === 'PROPOSED' && !planStep;
  const typed = needsTypedConfirm(a.risk);
  const locked = busy !== null || running;
  const threadClosed = closedThreads?.has(a.thread_id) ?? false;

  return (
    <>
      <Modal title={title} width={760} onClose={busy !== null ? undefined : onClose}>
        <Stack gap="var(--sg-s-6)">
          <Row>
            <span style={{ ...mono, fontWeight: 700, overflowWrap: 'anywhere' }}>{a.tool}</span>
            <StatusChip tone={riskTone(a.risk)} title={a.risk}>
              {riskLabel(a.risk)}
            </StatusChip>
            <StatusChip tone={actionStatusTone(a.status)} title={String(a.status)}>
              {tr.debate.actionStatus[status] ?? String(a.status)}
            </StatusChip>
            {threadClosed ? <StatusChip tone="idle">{tr.debate.actions.threadClosed}</StatusChip> : null}
          </Row>
          {threadClosed ? <div style={{ ...muted, fontSize: 12 }}>{tr.debate.dialog.threadClosedNote}</div> : null}
          {a.tool_description ? <div style={{ color: 'var(--sg-text-secondary)', lineHeight: 1.5 }}>{a.tool_description}</div> : null}
          <Stack gap="var(--sg-s-2)">
            <Caption>{tr.debate.actions.colReason}</Caption>
            <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.5 }}>{a.reason || tr.debate.details.none}</div>
          </Stack>
          <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
            {tr.debate.dialog.threadLine(a.thread_id, a.round_num)}
          </div>
          {a.base_sha ? (
            <div style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
              {`${tr.debate.dialog.baseCommit}: `}
              <span style={mono} title={a.base_sha}>
                {a.base_sha.slice(0, 7)}
              </span>
            </div>
          ) : null}
          {changesWorkspace(a.tool) ? (
            <Notice tone="warn">
              {tr.debate.dialog.workspaceWarn}
            </Notice>
          ) : null}
          <Stack gap="var(--sg-s-2)">
            <Caption>{tr.debate.dialog.arguments}</Caption>
            <ActionArguments action={a} />
          </Stack>
          {a.preview ? (
            <Stack gap="var(--sg-s-2)">
              <Caption>{tr.debate.details.preview}</Caption>
              <ActionPreviewView preview={a.preview} />
            </Stack>
          ) : null}
          {a.result !== null && a.result !== undefined && a.result !== '' ? (
            <Stack gap="var(--sg-s-2)">
              <Caption>{ranNow ? tr.debate.dialog.resultNow : tr.debate.dialog.resultPrev}</Caption>
              <ActionResult result={a.result} />
            </Stack>
          ) : null}
          <div style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
            {a.decided_by ? tr.debate.dialog.decidedBy(a.decided_by) : tr.debate.dialog.notDecided}
            {a.updated_at ? ` · ${tr.debate.dialog.updated(fmtTime(a.updated_at))}` : ''}
            {a.created_at ? ` · ${tr.debate.dialog.created(fmtTime(a.created_at))}` : ''}
          </div>

          {planStep && a.plan_id ? (
            <Notice tone="warn">
              {tr.debate.plans.stepOf(a.plan_id)}{' '}
              <Button size="sm" variant="outline" onClick={() => navigate('approvals', [a.plan_id as string])}>
                {tr.debate.plans.openPlan}
              </Button>
            </Notice>
          ) : null}
          {isProposed ? (
            <Row>
              <Button variant="primary" loading={running} disabled={locked} onClick={() => setConfirmRun(true)}>
                {running ? tr.debate.dialog.executing : tr.debate.dialog.execute}
              </Button>
              <Button variant="outline" loading={busy === 'reject'} disabled={locked} onClick={() => setConfirmReject(true)}>
                {busy === 'reject' ? tr.debate.dialog.rejecting : tr.debate.dialog.reject}
              </Button>
              {running ? <span style={{ color: 'var(--sg-text-secondary)' }}>{tr.debate.dialog.runningNote}</span> : null}
            </Row>
          ) : null}
          {status === 'HELD' ? (
            <Stack gap="var(--sg-s-3)">
              <Notice tone="warn">
                {tr.debate.dialog.heldNote}
              </Notice>
              <div>
                <Button variant="primary" loading={busy === 'recheck'} disabled={locked || !THREAD_ID_RE.test(a.thread_id)} onClick={() => void doRecheck(a)}>
                  {busy === 'recheck' ? tr.debate.dialog.checking : tr.debate.dialog.recheckFor(a.thread_id)}
                </Button>
              </div>
            </Stack>
          ) : null}
          {status === 'RUNNING' ? (
            <Stack gap="var(--sg-s-3)">
              <Notice tone="warn">
                {tr.debate.dialog.runningStateNote}
              </Notice>
              {!running ? (
                <div>
                  <Button variant="outline" loading={busy === 'fail'} disabled={locked} onClick={() => setConfirmFail(true)}>
                    {busy === 'fail' ? tr.debate.dialog.marking : tr.debate.dialog.markFailed}
                  </Button>
                </div>
              ) : null}
            </Stack>
          ) : null}

          {recheck ? <RecheckSummary result={recheck} /> : null}
          {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button variant="ghost" disabled={busy !== null} onClick={onClose}>
              {tr.common.close}
            </Button>
          </div>
        </Stack>
      </Modal>

      {confirmRun && isProposed && typed ? (
        <ConfirmDialog
          risk="HIGH"
          title={tr.debate.dialog.executeQ(a.id)}
          confirmText={String(a.id)}
          confirmLabel={tr.debate.dialog.executeAction(a.id)}
          onConfirm={() => void doRun(a)}
          onCancel={() => setConfirmRun(false)}
        >
          <RunSummary action={a} />
          <ScrollBox>
            <ActionArguments action={a} />
          </ScrollBox>
        </ConfirmDialog>
      ) : null}

      {confirmRun && isProposed && !typed ? (
        <Modal title={tr.debate.dialog.confirmExecution(a.id)} width={640} onClose={() => setConfirmRun(false)}>
          <Stack gap="var(--sg-s-5)">
            <RunSummary action={a} />
            <ScrollBox>
              <ActionArguments action={a} />
            </ScrollBox>
            <CheckboxField checked={reviewed} onChange={(e) => setReviewed(e.target.checked)}>
              {tr.debate.dialog.reviewed}
            </CheckboxField>
            <Row>
              <Button variant="danger" disabled={!reviewed} onClick={() => void doRun(a)}>
                {tr.debate.dialog.executeAction(a.id)}
              </Button>
              <Button variant="ghost" onClick={() => setConfirmRun(false)}>
                {tr.common.back}
              </Button>
            </Row>
          </Stack>
        </Modal>
      ) : null}

      {confirmFail ? (
        <ConfirmModal
          title={tr.debate.dialog.markFailedQ(a.id)}
          message={tr.debate.dialog.markFailedMsg(a.id, a.tool)}
          confirmLabel={tr.debate.dialog.markFailed}
          onConfirm={() => void doFailStale(a)}
          onCancel={() => setConfirmFail(false)}
        />
      ) : null}

      {confirmReject ? (
        <ConfirmModal
          title={tr.debate.dialog.rejectQ(a.id)}
          message={tr.debate.dialog.rejectMsg(a.id, a.tool)}
          confirmLabel={tr.debate.dialog.rejectAction}
          onConfirm={() => void doReject(a)}
          onCancel={() => setConfirmReject(false)}
        />
      ) : null}
    </>
  );
}

import { useState } from 'react';
import { Button } from '@server-guardian/ui';
import { AuthRequiredError, HubError, getWorkspaceReset, resetWorkspace } from '../../api';
import { CheckboxField } from '../../components/CheckboxField';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataGate } from '../../components/DataGate';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { errorText } from '../../lib/format';
import type { WorkspaceResetPlan, WorkspaceResetResult } from '../../types';
import { tr } from '../../i18n';

/** Clear wording for the two refusals the hub documents; anything else is shown as sent. */
export function resetErrorText(err: unknown): string {
  if (err instanceof HubError && err.status === 409) {
    return tr.debate.reset.busy(err.message);
  }
  if (err instanceof HubError && err.status === 403) {
    return `${err.message}\n${tr.debate.reset.uiOff}`;
  }
  return errorText(err);
}

/** Kept files grouped by the thread that owns them (files without a thread id share one group, which cannot be released). */
export function groupKept(keep: WorkspaceResetPlan['keep']): Array<{ thread: string; files: WorkspaceResetPlan['keep'] }> {
  const groups = new Map<string, WorkspaceResetPlan['keep']>();
  for (const f of keep) {
    const key = f.thread_id ?? '';
    groups.set(key, [...(groups.get(key) ?? []), f]);
  }
  return [...groups].map(([thread, files]) => ({ thread, files }));
}

/**
 * Discards the uncommitted changes in a project's hub workspace. The plan (GET) lists the files that would be thrown
 * away with their state and the ones kept because an action still owns them; the operator confirms (danger), then the
 * result lists what was discarded.
 */
export function WorkspaceResetDialog({ projectId, onClose, onDone }: { projectId: string; onClose: () => void; onDone: () => void }) {
  const plan = useHubData(() => getWorkspaceReset(projectId));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [release, setRelease] = useState<string[]>([]);
  const [result, setResult] = useState<WorkspaceResetResult | null>(null);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(release.length ? await resetWorkspace(projectId, release) : await resetWorkspace(projectId));
      onDone();
    } catch (err) {
      if (err instanceof AuthRequiredError) window.dispatchEvent(new CustomEvent('guardian:auth-required'));
      else setError(resetErrorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    return (
      <Modal title={tr.debate.reset.doneTitle(projectId)} width={560} onClose={onClose}>
        <Stack gap="var(--sg-s-5)">
          <Notice tone="ok">{tr.debate.reset.discarded(result.discarded.length)}</Notice>
          {result.discarded.length ? (
            <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', ...mono, fontSize: 13, overflowWrap: 'anywhere' }}>
              {result.discarded.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : null}
          <Row style={{ justifyContent: 'flex-end' }}>
            <Button variant="primary" onClick={onClose}>
              {tr.debate.reset.done}
            </Button>
          </Row>
        </Stack>
      </Modal>
    );
  }

  return (
    <ConfirmModal
      title={tr.debate.reset.title(projectId)}
      confirmLabel={tr.debate.project.resetWorkspace}
      confirmDisabled={plan.data === null}
      busy={busy}
      error={error}
      message={
        <Stack gap="var(--sg-s-4)">
          <DataGate state={plan} what={tr.debate.reset.what}>
            {(p) => (
              <Stack gap="var(--sg-s-4)">
                <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{p.workspace}</div>
                <div>
                  <strong>{tr.debate.reset.willDiscard(p.discard.length)}</strong>
                  {p.discard.length ? (
                    <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', ...mono, fontSize: 13, overflowWrap: 'anywhere' }}>
                      {p.discard.map((f) => (
                        <li key={f.path}>{`${f.path} (${f.state})`}</li>
                      ))}
                    </ul>
                  ) : (
                    <div style={muted}>{tr.debate.reset.nothing}</div>
                  )}
                </div>
                <div>
                  <strong>{tr.debate.reset.kept(p.keep.length)}</strong>
                  {p.keep.length ? (
                    <Stack gap="var(--sg-s-4)">
                      {groupKept(p.keep).map((g) => (
                        <div key={g.thread} data-testid={`kept-${g.thread || 'none'}`}>
                          {g.thread ? <div style={{ ...mono, fontSize: 13, overflowWrap: 'anywhere' }}>{g.thread}</div> : null}
                          <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', ...mono, fontSize: 13, overflowWrap: 'anywhere' }}>
                            {g.files.map((f) => (
                              <li key={f.path}>{`${f.path} (${tr.debate.reset.actionRef(f.action_id)})`}</li>
                            ))}
                          </ul>
                          {g.thread ? (
                            <CheckboxField
                              checked={release.includes(g.thread)}
                              disabled={busy}
                              onChange={(e) => setRelease((r) => (e.target.checked ? [...r, g.thread] : r.filter((t) => t !== g.thread)))}
                            >
                              <span style={{ color: 'var(--sg-crit)' }}>{tr.debate.reset.release(g.thread)}</span>
                              <span style={{ ...muted, display: 'block', fontSize: 12 }}>
                                {tr.debate.reset.releaseNote}
                              </span>
                            </CheckboxField>
                          ) : null}
                        </div>
                      ))}
                    </Stack>
                  ) : (
                    <div style={muted}>{tr.debate.reset.noneKept}</div>
                  )}
                </div>
              </Stack>
            )}
          </DataGate>
          <span>{tr.debate.reset.irreversible}</span>
        </Stack>
      }
      onConfirm={() => void confirm()}
      onCancel={onClose}
    />
  );
}

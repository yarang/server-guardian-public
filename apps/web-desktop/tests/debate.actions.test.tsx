import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import { action } from './debateFixtures';

const D = ko.debate;

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { ActionDialog } from '../src/pages/debate/ActionDialog';
import { OperationsStoreProvider } from '../src/state/operationsStore';
import type { ActionItem, ActionRunResponse } from '../src/types';

const mocked = vi.mocked(api);
const onClose = vi.fn();
const onChanged = vi.fn();

async function open(a: ActionItem) {
  mocked.getAction.mockResolvedValue(a);
  const view = render(
    <GuardianRoot>
      <OperationsStoreProvider>
        <ActionDialog id={a.id} onClose={onClose} onChanged={onChanged} />
      </OperationsStoreProvider>
    </GuardianRoot>
  );
  await screen.findByRole('dialog', { name: D.dialog.title(a.id) });
  return view;
}
const detail = (id = 57) => screen.getByRole('dialog', { name: D.dialog.title(id) });
const flush = () => act(async () => undefined);

beforeEach(() => {
  for (const fn of [mocked.getAction, mocked.runAction, mocked.rejectAction, mocked.failStaleAction, mocked.jevRecheck]) fn.mockReset();
  onClose.mockReset();
  onChanged.mockReset();
});

describe('Action detail', () => {
  it('shows tool, risk, description, reason, base commit and the write-tool note', async () => {
    await open(
      action({
        tool: 'jev_mcp:execute_code_action',
        risk: 'medium',
        reason: 'Fix the backoff',
        base_sha: 'abcdef0123456789',
        arguments: { file_path: 'apps/agent/sse.go', run_test: 'go test ./...', content: 'package main\n// full file' },
      })
    );
    const d = detail();
    expect(within(d).getByText('jev_mcp:execute_code_action')).toBeTruthy();
    expect(within(d).getByText(D.lib.risk.medium)).toBeTruthy();
    expect(within(d).getByText(D.actionStatus.PROPOSED)).toBeTruthy();
    expect(within(d).getByText('Runs tests on the hub workspace')).toBeTruthy();
    expect(within(d).getByText('Fix the backoff')).toBeTruthy();
    expect(within(d).getByText('abcdef0').title).toBe('abcdef0123456789');
    expect(within(d).getByText(D.dialog.workspaceWarn)).toBeTruthy();
    expect(within(d).getByText('apps/agent/sse.go')).toBeTruthy();
    expect(within(d).getByText(/package main/).textContent).toContain('// full file');
  });

  it('renders an apply_patch as a summary, a diff and collapsible raw edits', async () => {
    await open(
      action({
        tool: 'jev_mcp:apply_patch',
        risk: 'medium',
        arguments: { edits: [{ file_path: 'a.go', old: 'x', new: 'y' }, { file_path: 'b.go', old: '', new: 'z' }], check: true },
        diff: '--- a/a.go\n+++ b/a.go\n@@ -1 +1 @@\n-x\n+y',
      })
    );
    const d = detail();
    expect(within(d).getByText(D.lib.patchSummary(2, 2))).toBeTruthy();
    const diff = within(d).getByRole('region', { name: /diff/i });
    const kinds = Array.from(diff.children).map((row) => row.getAttribute('data-diff'));
    expect(kinds).toEqual(['header', 'header', 'hunk', 'del', 'add']);
    expect(within(d).getByText(D.details.rawEdits)).toBeTruthy();
    expect(within(d).getByText(`#2 b.go${D.details.newFile}`)).toBeTruthy();
  });

  it('shows diff text, including markup, as plain text', async () => {
    const { container } = await open(
      action({
        tool: 'jev_mcp:apply_patch',
        arguments: { edits: [{ file_path: 'a.html', old: '', new: '<script>alert(1)</script>' }] },
        diff: '--- a/a.html\n+++ b/a.html\n@@ -0,0 +1 @@\n+<script>alert(1)</script>\n+<img src=x onerror=alert(2)>',
      })
    );
    const diff = within(detail()).getByRole('region', { name: /diff/i });
    expect(within(diff).getByText('+<script>alert(1)</script>')).toBeTruthy();
    expect(within(diff).getByText('+<img src=x onerror=alert(2)>')).toBeTruthy();
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });

  it('shows a previous result with a reverted banner, the checks table and outputs', async () => {
    await open(
      action({
        status: 'FAILED',
        tool: 'jev_mcp:apply_patch',
        arguments: { edits: [] },
        result: {
          status: 'FAILED',
          message: 'checks failed',
          error: 'go build failed',
          reverted: true,
          diff: '-a\n+b',
          checks: [{ cmd: 'go build ./...', cwd: 'apps/agent', exit_code: 2, stdout: 'out text', stderr: 'err text' }],
          exit_code: 1,
          stdout: 'plain stdout',
          stderr: 'plain stderr',
        },
      })
    );
    const d = detail();
    expect(within(d).getByText(D.dialog.resultPrev)).toBeTruthy();
    expect(within(d).getByRole('alert').textContent).toBe(D.details.reverted);
    expect(within(d).getByText('exit_code: 1')).toBeTruthy();
    expect(within(d).getByText('checks failed')).toBeTruthy();
    expect(within(d).getByText('go build failed')).toBeTruthy();
    expect(within(d).getByRole('region', { name: D.details.appliedDiff })).toBeTruthy();
    const checks = within(d).getByRole('table', { name: D.details.checks });
    expect(within(checks).getByText('go build ./...')).toBeTruthy();
    expect(within(checks).getByText('2')).toBeTruthy();
    expect(within(checks).getByText('out text')).toBeTruthy();
    expect(within(d).getByText('plain stdout')).toBeTruthy();
    expect(within(d).getByText('plain stderr')).toBeTruthy();
  });

  it('shows a load failure', async () => {
    mocked.getAction.mockRejectedValue(new api.HubError('no such action', 404));
    render(
      <GuardianRoot>
        <OperationsStoreProvider>
          <ActionDialog id={9} onClose={onClose} onChanged={onChanged} />
        </OperationsStoreProvider>
      </GuardianRoot>
    );
    expect((await screen.findByRole('alert')).textContent).toBe(D.dialog.loadFailed(9, 'no such action'));
  });
});

describe('Running an action', () => {
  it('low risk: needs the "I reviewed the arguments" checkbox before Execute is enabled', async () => {
    mocked.runAction.mockResolvedValue({
      ok: true,
      action: action({ status: 'EXECUTED', result: { exit_code: 0, stdout: 'ok all green' }, decided_by: 'dashboard@100.64.0.7' }),
    });
    await open(action({ risk: 'low' }));
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));

    const confirm = screen.getByRole('dialog', { name: D.dialog.confirmExecution(57) });
    const go = within(confirm).getByRole('button', { name: D.dialog.executeAction(57) }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    expect(within(confirm).queryByRole('textbox')).toBeNull();
    fireEvent.click(go);
    expect(mocked.runAction).not.toHaveBeenCalled();

    fireEvent.click(within(confirm).getByLabelText(D.dialog.reviewed));
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await flush();

    expect(mocked.runAction).toHaveBeenCalledTimes(1);
    expect(mocked.runAction).toHaveBeenCalledWith(57);
    expect(screen.queryByRole('dialog', { name: D.dialog.confirmExecution(57) })).toBeNull();
    const d = detail();
    expect(within(d).getByText(D.dialog.finished(57, 'EXECUTED'))).toBeTruthy();
    expect(within(d).getByText(D.dialog.resultNow)).toBeTruthy();
    expect(within(d).getByText('ok all green')).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
  });

  it('medium risk also uses the checkbox', async () => {
    await open(action({ risk: 'medium' }));
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));
    const confirm = screen.getByRole('dialog', { name: D.dialog.confirmExecution(57) });
    expect(within(confirm).getByLabelText(D.dialog.reviewed)).toBeTruthy();
  });

  it('high risk: Execute stays disabled until the exact action id is typed (no checkbox)', async () => {
    mocked.runAction.mockResolvedValue({ ok: true, action: action({ risk: 'high', status: 'EXECUTED', result: { exit_code: 0 } }) });
    await open(action({ risk: 'high', tool: 'server_remediation', arguments: { target_host: 'site-b-ec1', command: 'systemctl reload ssh' } }));
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));

    const confirm = screen.getByRole('alertdialog', { name: D.dialog.executeQ(57) });
    expect(within(confirm).queryByLabelText(D.dialog.reviewed)).toBeNull();
    expect(confirm.textContent).toContain('server_remediation (' + D.dialog.riskLabel + ': high) · ' + D.dialog.targetNode + ': site-b-ec1');
    const go = within(confirm).getByRole('button', { name: D.dialog.executeAction(57) }) as HTMLButtonElement;
    const typed = within(confirm).getByRole('textbox');
    expect(go.disabled).toBe(true);

    fireEvent.change(typed, { target: { value: '5' } });
    expect(go.disabled).toBe(true);
    fireEvent.change(typed, { target: { value: '570' } });
    expect(go.disabled).toBe(true);
    fireEvent.change(typed, { target: { value: '57' } });
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await flush();
    expect(mocked.runAction).toHaveBeenCalledWith(57);
  });

  it('an unknown risk is treated like high', async () => {
    await open(action({ risk: undefined }));
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));
    expect(screen.getByRole('alertdialog')).toBeTruthy();
  });

  it('Back leaves the action unrun', async () => {
    await open(action());
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));
    fireEvent.click(screen.getByRole('button', { name: ko.common.back }));
    expect(screen.queryByRole('dialog', { name: D.dialog.confirmExecution(57) })).toBeNull();
    expect(mocked.runAction).not.toHaveBeenCalled();
  });

  it('locks the buttons and says it can take minutes while the run is in flight', async () => {
    let finish: (v: ActionRunResponse) => void = () => undefined;
    mocked.runAction.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    await open(action());
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));
    fireEvent.click(screen.getByLabelText(D.dialog.reviewed));
    fireEvent.click(screen.getByRole('button', { name: D.dialog.executeAction(57) }));
    await flush();

    const d = detail();
    expect(within(d).getByText(D.dialog.runningNote)).toBeTruthy();
    expect((within(d).getByRole('button', { name: D.dialog.executing }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(d).getByRole('button', { name: D.dialog.reject }) as HTMLButtonElement).disabled).toBe(true);
    // The run lives in the operations store, so the dialog may be closed while it is in flight.
    expect((within(d).getByRole('button', { name: ko.common.close }) as HTMLButtonElement).disabled).toBe(false);

    finish({ ok: true, action: action({ status: 'EXECUTED', result: { exit_code: 0 } }) });
    await flush();
    expect((within(detail()).getByRole('button', { name: ko.common.close }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('shows a failed run (tool failure) as an error with the result', async () => {
    mocked.runAction.mockResolvedValue({ ok: false, action: action({ status: 'FAILED', result: { exit_code: 3, stderr: 'boom' } }) });
    await open(action());
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));
    fireEvent.click(screen.getByLabelText(D.dialog.reviewed));
    fireEvent.click(screen.getByRole('button', { name: D.dialog.executeAction(57) }));
    await flush();
    expect(within(detail()).getByText(D.dialog.finishedWith(57, 'FAILED'))).toBeTruthy();
    expect(within(detail()).getByText('boom')).toBeTruthy();
  });

  async function runExpectingError(error: api.HubError) {
    mocked.runAction.mockRejectedValue(error);
    await open(action());
    mocked.getAction.mockClear();
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.execute }));
    fireEvent.click(screen.getByLabelText(D.dialog.reviewed));
    fireEvent.click(screen.getByRole('button', { name: D.dialog.executeAction(57) }));
    await flush();
    return within(detail()).getByRole('alert').textContent ?? '';
  }

  it('409 "workspace moved" suggests a new debate and refreshes the detail', async () => {
    const text = await runExpectingError(new api.HubError('workspace moved from abc1234 to def5678; debate again on the current code', 409));
    expect(text).toContain('workspace moved from abc1234 to def5678');
    expect(text).toContain(D.lib.workspaceMoved);
    expect(mocked.getAction).toHaveBeenCalledWith(57);
  });

  it('409 for another state change explains the action is no longer runnable', async () => {
    const text = await runExpectingError(new api.HubError('action 57 is EXECUTED', 409));
    expect(text).toContain(D.lib.notRunnable);
  });

  it('403 says UI actions are disabled on the hub', async () => {
    const text = await runExpectingError(new api.HubError('forbidden', 403));
    expect(text).toContain(D.lib.uiActionsOff('').slice(0, 20));
    expect(text).toContain('guardian.py --run-action');
  });

  it('400 and 404 are labelled', async () => {
    expect(await runExpectingError(new api.HubError('confirm_id mismatch', 400))).toBe(D.lib.invalidRequest('confirm_id mismatch'));
  });
});

describe('Rejecting an action', () => {
  it('asks for confirmation, then rejects', async () => {
    mocked.rejectAction.mockResolvedValue(action({ status: 'REJECTED', decided_by: 'dashboard@100.64.0.7' }));
    await open(action());
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.reject }));
    expect(mocked.rejectAction).not.toHaveBeenCalled();
    const confirm = screen.getByRole('dialog', { name: D.dialog.rejectQ(57) });
    fireEvent.click(within(confirm).getByRole('button', { name: D.dialog.rejectAction }));
    await flush();
    expect(mocked.rejectAction).toHaveBeenCalledWith(57);
    expect(within(detail()).getByText(D.dialog.rejected(57))).toBeTruthy();
    expect(within(detail()).queryByRole('button', { name: D.dialog.execute })).toBeNull();
  });
});

describe('Stale RUNNING actions', () => {
  it('confirms, then marks a RUNNING action as failed', async () => {
    mocked.failStaleAction.mockResolvedValue(action({ status: 'FAILED' }));
    await open(action({ status: 'RUNNING' }));
    expect(within(detail()).queryByRole('button', { name: D.dialog.execute })).toBeNull();
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.markFailed }));
    expect(mocked.failStaleAction).not.toHaveBeenCalled();
    const confirm = screen.getByRole('dialog', { name: D.dialog.markFailedQ(57) });
    fireEvent.click(within(confirm).getByRole('button', { name: D.dialog.markFailed }));
    await flush();
    expect(mocked.failStaleAction).toHaveBeenCalledWith(57);
    expect(within(detail()).getByText(D.dialog.markedFailed(57))).toBeTruthy();
  });

  it('shows the hub refusal when the hub is still running the action', async () => {
    mocked.failStaleAction.mockRejectedValue(new api.HubError('this hub is still running the action; wait for it to finish', 409));
    mocked.getAction.mockResolvedValue(action({ status: 'RUNNING' }));
    await open(action({ status: 'RUNNING' }));
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.markFailed }));
    fireEvent.click(within(screen.getByRole('dialog', { name: D.dialog.markFailedQ(57) })).getByRole('button', { name: D.dialog.markFailed }));
    await flush();
    expect(within(detail()).getByText(/still running the action/)).toBeTruthy();
  });

  it('offers no Mark as failed button on a PROPOSED action', async () => {
    await open(action());
    expect(within(detail()).queryByRole('button', { name: D.dialog.markFailed })).toBeNull();
  });
});

describe('HELD actions', () => {
  const held = () => action({ status: 'HELD', risk: 'medium' });

  it('explains the hold, offers no Run, and re-runs the jev check for the thread', async () => {
    mocked.jevRecheck.mockResolvedValue({ ok: true, gate: 'PASSED', released: [57], review: { reasons: ['all constraints satisfied'] } });
    await open(held());
    const d = detail();
    expect(within(d).getByText(D.dialog.heldNote)).toBeTruthy();
    expect(within(d).queryByRole('button', { name: D.dialog.execute })).toBeNull();
    expect(within(d).queryByRole('button', { name: D.dialog.reject })).toBeNull();

    mocked.getAction.mockResolvedValue(action({ status: 'PROPOSED', risk: 'medium' }));
    fireEvent.click(within(d).getByRole('button', { name: D.dialog.recheckFor('thread_abc123') }));
    await flush();

    expect(mocked.jevRecheck).toHaveBeenCalledWith('thread_abc123');
    expect(within(detail()).getByText(new RegExp('^' + D.details.released + ':')).textContent).toBe(D.details.released + ': #57');
    expect(within(detail()).getByText('all constraints satisfied')).toBeTruthy();
    // The refreshed action is now PROPOSED and runnable.
    expect(within(detail()).getByRole('button', { name: D.dialog.execute })).toBeTruthy();
    expect(onChanged).toHaveBeenCalled();
  });

  it('shows a recheck failure', async () => {
    mocked.jevRecheck.mockRejectedValue(new api.HubError('jev not reachable', 409));
    await open(held());
    fireEvent.click(within(detail()).getByRole('button', { name: D.dialog.recheckFor('thread_abc123') }));
    await flush();
    expect(within(detail()).getByRole('alert').textContent).toContain('jev not reachable');
  });
});

describe('Ops cleanup actions', () => {
  const cleanup: ActionItem = action({
    id: 61,
    tool: 'ops:apt_clean',
    risk: 'low',
    arguments: { target_host: 'site-a-ec1' },
    preview: { bytes: 300 * 1024 ** 2, text: '307200\t/var/cache/apt/archives', error: null },
  });

  it('shows what a proposed cleanup would delete', async () => {
    await open(cleanup);
    const d = detail(61);
    expect(within(d).getByText(D.details.preview)).toBeTruthy();
    expect(within(d).getByText(D.details.previewFrees('300.0 MB'))).toBeTruthy();
    expect(within(d).getByText('307200\t/var/cache/apt/archives', { normalizer: (s) => s })).toBeTruthy();
  });

  it('shows the before and after measurement of an executed cleanup and flags a stale one', async () => {
    await open({
      ...cleanup,
      status: 'EXECUTED',
      result: { status: 'SUCCESS', exit_code: 0, verify: { before: 2 * 1024 ** 3, after: 4096, freed: 2 * 1024 ** 3 - 4096, ok: true } },
    });
    expect(within(detail(61)).getByText(D.details.verifyLine('2.0 GB', '4.0 KB', '2.0 GB'))).toBeTruthy();
  });

  it('labels a stale cleanup', async () => {
    await open({ ...cleanup, status: 'STALE', result: { error: 'it would now delete 2.0 GiB, 1.0 MiB when it was proposed' } });
    expect(within(detail(61)).getAllByText(D.actionStatus.STALE).length).toBeGreaterThan(0);
  });
});

describe('write_artifact proposal', () => {
  const proposal = (args: Record<string, unknown>) =>
    action({ id: 108, tool: 'jev_mcp:write_artifact', risk: 'medium', arguments: args });

  it('draws Markdown as a document instead of an escaped JSON string', async () => {
    await open(
      proposal({
        name: 'ui-review.md',
        base_version: 'artifact@v1',
        content: '# Review\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- first\n- second\n',
      })
    );
    const d = detail(108);
    expect(within(d).getByText('ui-review.md')).toBeTruthy();
    expect(within(d).getByText('artifact@v1')).toBeTruthy();
    expect(within(d).getByRole('heading', { name: 'Review' })).toBeTruthy();
    expect(within(d).getByRole('table')).toBeTruthy();
    expect(within(d).getAllByRole('listitem')).toHaveLength(2);
    // The raw text sits behind a disclosure, not in the preview.
    expect(within(d).getByText(D.details.artifactRaw)).toBeTruthy();
  });

  it('says so when there is no base version yet', async () => {
    await open(proposal({ name: 'a.md', content: '# A\n' }));
    expect(within(detail(108)).getByText(D.details.artifactFirstSave)).toBeTruthy();
  });

  it('keeps hostile Markdown inert', async () => {
    const { container } = await open(
      proposal({ name: 'x.md', content: '# T\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n' })
    );
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img[onerror]')).toBeNull();
  });

  it('shows SVG only as an image, never as markup', async () => {
    const { container } = await open(
      proposal({ name: 'logo.svg', content: '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/><script>alert(1)</script></svg>' })
    );
    const img = within(detail(108)).getByRole('img');
    expect(img.getAttribute('src')).toMatch(/^data:image\/svg\+xml;base64,/);
    expect(container.querySelector('svg circle')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });

  it('falls back to the raw text for a name it cannot draw', async () => {
    await open(proposal({ name: 'notes.txt', content: 'plain text' }));
    const d = detail(108);
    expect(within(d).getAllByText(D.details.none).length).toBeGreaterThan(0);
    expect(within(d).getByText(D.details.artifactRaw)).toBeTruthy();
  });
});

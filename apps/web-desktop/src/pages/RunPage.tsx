import { tr } from '../i18n';
import { useEffect, useState } from 'react';
import { Button, CodeBlock, Meter, Panel, StatusChip, Terminal, TextField } from '@server-guardian/ui';
import type { Tone } from '@server-guardian/ui';
import { DEFAULT_FLEET_TIMEOUT_SEC } from '../api';
import { ConfirmModal } from '../components/ConfirmModal';
import { ErrorNotice } from '../components/DataGate';
import { DataTable } from '../components/DataTable';
import type { Column } from '../components/DataTable';
import { Row, Stack } from '../components/layout';
import { SelectField } from '../components/SelectField';
import { useHubStore } from '../state/hubStore';
import { ALL_TARGET, GROUP_PREFIX, LOCAL_TARGET, useRunStore } from '../state/runStore';
import type { RowStatus, RunRow } from '../state/runStore';
import { CommandHistory } from './run/CommandHistory';

/**
 * The hub only rejects timeout_sec <= 0 (it falls back to 10 s) and has no upper clamp, so the
 * UI bounds the per-node timeout itself: 1 s to 10 min.
 */
export const FLEET_TIMEOUT_MIN_SEC = 1;
export const FLEET_TIMEOUT_MAX_SEC = 600;

/** Parses the timeout field; an empty or invalid entry falls back to the default, others are clamped. */
export function parseFleetTimeout(text: string): number {
  const n = Math.round(Number(text));
  if (text.trim() === '' || !Number.isFinite(n)) return DEFAULT_FLEET_TIMEOUT_SEC;
  return Math.min(FLEET_TIMEOUT_MAX_SEC, Math.max(FLEET_TIMEOUT_MIN_SEC, n));
}

const PRESETS = ['uname -m', 'uptime', 'df -h /', 'free -h'];
// Progress is not a usage level: never let the Meter turn amber/red as it fills.
const PROGRESS_THRESHOLDS = { warn: 101, crit: 101 };

const STATUS_TONE: Record<RowStatus, Tone> = {
  WAITING: 'idle',
  RUNNING: 'info',
  SUCCESS: 'ok',
  FAILED: 'crit',
  'NO RESULT': 'warn',
};

const ROW_COLUMNS: Column<RunRow>[] = [
  { header: tr.ops.run.results.node, render: (r) => <strong>{r.name}</strong> },
  { header: tr.ops.run.results.status, render: (r) => <StatusChip tone={STATUS_TONE[r.status]} title={r.status}>{tr.ops.run.rowStatus[r.status] ?? r.status}</StatusChip> },
  { header: tr.ops.run.results.exit, mono: true, render: (r) => (r.exitCode === undefined ? '—' : r.exitCode) },
  { header: tr.ops.run.results.duration, mono: true, align: 'right', render: (r) => (r.durationMs === undefined ? '—' : `${r.durationMs} ms`) },
  { header: tr.ops.run.results.output, mono: true, render: (r) => r.output || '—' },
];

/**
 * Run: one target picker (local node, one node, a group, the whole fleet). A single target behaves
 * like a terminal (history kept, no confirmation); a group or fleet run asks for confirmation and
 * streams progress. All run state lives in RunStoreProvider, so leaving and returning keeps it.
 * `target` (from `?target=`) preselects the picker unless a group / fleet run is in progress.
 */
export function RunPage({ target: urlTarget }: { target?: string } = {}) {
  const { servers, groups } = useHubStore();
  const run = useRunStore();
  const { target, offerTarget } = run;
  const [pending, setPending] = useState<string | null>(null);
  const [timeoutText, setTimeoutText] = useState(String(DEFAULT_FLEET_TIMEOUT_SEC));
  const timeoutSec = parseFleetTimeout(timeoutText);

  useEffect(() => {
    offerTarget(urlTarget);
  }, [urlTarget, offerTarget]);

  const list = servers.data ?? [];
  const groupList = groups.data?.groups ?? [];
  const group = target.startsWith(GROUP_PREFIX) ? groupList.find((g) => GROUP_PREFIX + g.id === target) ?? null : null;
  // Fleet and group runs stream over many nodes and need confirmation; a single target does not.
  const multi = target === ALL_TARGET || target.startsWith(GROUP_PREFIX);
  const label =
    target === ALL_TARGET
      ? tr.ops.run.labelFleet
      : target === LOCAL_TARGET
        ? tr.ops.run.labelLocal
        : group
          ? group.name
          : list.find((s) => s.id === target)?.name ?? target;
  const state = run.multi;
  const busy = run.multiRunning || run.singleRunning;

  const start = (command: string) => {
    const cmd = command.trim();
    if (!cmd) return;
    if (!multi) {
      // Single targets can be typed repeatedly; the store reports "still running" in the history.
      run.runSingle(target, label, cmd);
      return;
    }
    if (busy) return;
    if (target.startsWith(GROUP_PREFIX) && !group) return; // group vanished on reload; never run it as a host name
    setPending(cmd);
  };

  // A group is sent as its resolved node ids; the hub checks each against servers.json. The same rows are listed
  // in the confirmation, so what the operator reads is what runs.
  const nameOf = new Map(list.map((s) => [s.id, s.name]));
  const resolved = group ? group.members.map((id) => ({ id, name: nameOf.get(id) ?? id })) : list.map((s) => ({ id: s.id, name: s.name }));

  const execute = (command: string) => {
    run.runMulti({ label, command, ids: group ? group.members : [], rows: resolved, timeoutSec });
  };

  const options = [
    { value: LOCAL_TARGET, label: tr.ops.run.targetHub },
    ...list.map((s) => ({ value: s.id, label: `${s.name} (${s.hostname})` })),
    ...groupList.map((g) => ({
      value: GROUP_PREFIX + g.id,
      label: tr.ops.run.targetGroup(g.name, g.members.length),
      disabled: g.members.length === 0,
    })),
    { value: ALL_TARGET, label: tr.ops.run.targetAll },
  ];
  const rows = state.order.map((id) => state.rows[id]);
  const hasFailures = rows.some((r) => r.status === 'FAILED' || r.status === 'NO RESULT');
  const progress = state.total > 0 ? (state.completed / state.total) * 100 : 0;
  const showMulti = multi && state.phase !== 'idle';

  return (
    <Stack>
      {servers.error !== null ? (
        <ErrorNotice
          title={tr.ops.run.nodeListFailed}
          message={servers.error}
          onRetry={servers.reload}
          retrying={servers.loading}
        />
      ) : null}
      {groups.error !== null ? (
        <ErrorNotice
          title={tr.ops.run.groupsFailed}
          message={groups.error}
          onRetry={groups.reload}
          retrying={groups.loading}
        />
      ) : null}

      <Panel title={tr.ops.run.commands}>
        <Stack gap="var(--sg-s-6)">
          <Row gap="var(--sg-s-6)">
            <SelectField
              label={tr.ops.run.target}
              value={target}
              onChange={(e) => run.setTarget(e.target.value)}
              options={options}
              disabled={run.multiRunning}
            />
            <Row gap="var(--sg-s-3)">
              {PRESETS.map((p) => (
                <Button key={p} size="sm" variant="outline" disabled={busy} onClick={() => start(p)}>
                  {p}
                </Button>
              ))}
            </Row>
            {multi ? (
              <TextField
                label={tr.ops.run.timeoutPerNode}
                type="number"
                mono
                min={FLEET_TIMEOUT_MIN_SEC}
                max={FLEET_TIMEOUT_MAX_SEC}
                value={timeoutText}
                onChange={(e) => setTimeoutText(e.target.value)}
                onBlur={() => setTimeoutText(String(timeoutSec))}
                disabled={run.multiRunning}
              />
            ) : null}
            {!multi && run.singleRunning ? <StatusChip tone="info">{tr.ops.run.running}</StatusChip> : null}
          </Row>

          {showMulti ? (
            <Row gap="var(--sg-s-5)">
              {run.multiRunning ? <StatusChip tone="info">{tr.ops.run.running}</StatusChip> : null}
              {state.phase === 'done' ? (
                hasFailures ? <StatusChip tone="warn">{tr.ops.run.finishedWithFailures}</StatusChip> : <StatusChip tone="ok">{tr.ops.run.finished}</StatusChip>
              ) : null}
              {state.phase === 'error' ? <StatusChip tone="crit">{tr.ops.run.failed}</StatusChip> : null}
              {state.phase === 'cancelled' ? <StatusChip tone="warn">{tr.ops.run.stopped}</StatusChip> : null}
              <div style={{ width: 220 }}>
                <Meter value={progress} aria-label={tr.ops.run.batchProgress} showValue={false} thresholds={PROGRESS_THRESHOLDS} />
              </div>
              <span style={{ fontFamily: 'var(--sg-font-mono)', fontSize: 13 }}>
                {tr.ops.run.nodesProgress(state.completed, state.total)}
              </span>
              {run.multiRunning ? (
                <Button size="sm" variant="ghost" onClick={run.stopWatching}>
                  {tr.ops.run.stopWatching}
                </Button>
              ) : null}
            </Row>
          ) : null}

          {multi && state.error ? <ErrorNotice title={tr.ops.run.batchFailed} message={state.error} /> : null}

          {multi ? (
            <Terminal
              prompt={`${label} $`}
              lines={state.lines.length ? state.lines : [{ kind: 'meta', text: tr.ops.run.emptyTerminal }]}
              placeholder={tr.ops.run.multiPlaceholder}
              onRun={start}
            />
          ) : (
            <Terminal
              prompt="$"
              lines={run.history.length ? run.history : [{ kind: 'meta', text: tr.ops.run.ready(label) }]}
              placeholder={tr.ops.run.singlePlaceholder}
              onRun={start}
            />
          )}
        </Stack>
      </Panel>

      {multi ? (
        <Panel title={tr.ops.run.results.title}>
          <DataTable label={tr.ops.run.results.title} columns={ROW_COLUMNS} rows={rows} getKey={(r) => r.id} empty={tr.ops.run.results.empty} />
        </Panel>
      ) : null}

      <CommandHistory refreshKey={`${run.multi.phase}:${run.singleRunning ? 1 : 0}`} />

      {pending !== null ? (
        <ConfirmModal
          title={group ? tr.ops.run.confirm.titleGroup(group.name) : tr.ops.run.confirm.titleFleet}
          tone={group ? 'primary' : 'danger'}
          message={
            <Stack gap="var(--sg-s-4)">
              <span>
                {tr.ops.run.confirm.body(resolved.length, group?.name)}
              </span>
              <div aria-label={tr.ops.run.confirm.nodesAria} style={{ fontFamily: 'var(--sg-font-mono)', fontSize: 13, overflowWrap: 'anywhere' }}>
                {resolved.length ? resolved.map((r) => r.name).join(', ') : tr.ops.run.confirm.noNodes}
              </div>
              <CodeBlock code={pending} copyable={false} />
            </Stack>
          }
          confirmLabel={group ? tr.ops.run.confirm.confirmGroup : tr.ops.run.confirm.confirmFleet}
          confirmDisabled={resolved.length === 0}
          onConfirm={() => {
            const cmd = pending;
            setPending(null);
            execute(cmd);
          }}
          onCancel={() => setPending(null)}
        />
      ) : null}
    </Stack>
  );
}

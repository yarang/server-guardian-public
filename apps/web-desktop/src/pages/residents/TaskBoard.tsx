import { StatusChip } from '@server-guardian/ui';
import { Card } from '../../components/Card';
import { Grid, Row, Stack, mono, muted } from '../../components/layout';
import { actionStatusTone } from '../../lib/debate';
import { fmtBytes } from '../../lib/format';
import { hrefFor } from '../../router';
import { useOptionalApprovalsStore } from '../../state/approvalsStore';
import type { ResidentTask } from '../../types';
import { tr } from '../../i18n';
import { shortSha, taskStatusLabel } from './format';

/**
 * The board's statuses in display order: mail_tasks.py's open states (`pending`, `blocked`,
 * `in_progress`) then finished ones (`completed`, `failed`, `cancelled`, `expired`).
 */
const STATUS_ORDER = ['pending', 'blocked', 'in_progress', 'completed', 'failed', 'cancelled', 'expired'];

interface BoardColumn {
  key: string;
  label: string;
  tasks: ResidentTask[];
}

/** Columns of the board: the known statuses present, in the order above, then one bucket for the rest. */
export function boardColumns(tasks: ResidentTask[]): BoardColumn[] {
  const byStatus = new Map<string, ResidentTask[]>();
  for (const task of tasks) {
    const key = task.status && STATUS_ORDER.includes(task.status) ? task.status : 'other';
    const bucket = byStatus.get(key);
    if (bucket) bucket.push(task);
    else byStatus.set(key, [task]);
  }
  const known = STATUS_ORDER.filter((s) => byStatus.has(s)).map((s) => ({ key: s, label: taskStatusLabel(s), tasks: byStatus.get(s)! }));
  const other = byStatus.get('other');
  return other ? [...known, { key: 'other', label: tr.residents.otherStatus, tasks: other }] : known;
}

/**
 * One task card: title, who holds it, its pinned project and short sha, hop, call count, patch size.
 * A `patch_action` becomes a link to the Approvals screen with the action's real status chip —
 * read-only: nothing is approved or rejected from here.
 */
export function TaskCard({ task, showStatus = false }: { task: ResidentTask; showStatus?: boolean }) {
  const store = useOptionalApprovalsStore();
  const action = task.patch_action !== null ? (store?.actions ?? []).find((a) => a.id === task.patch_action) ?? null : null;
  const holder = task.owner ?? task.assignee;
  return (
    <Card>
      <strong style={{ overflowWrap: 'anywhere' }}>{task.title || task.id}</strong>
      <Row gap="var(--sg-s-3)">
        {showStatus ? (
          <StatusChip tone="idle" title={task.status ?? undefined}>
            {taskStatusLabel(task.status)}
          </StatusChip>
        ) : null}
        <span style={{ ...mono, ...muted, fontSize: 12 }}>{holder ? tr.residents.holder(holder) : tr.residents.noHolder}</span>
      </Row>
      <Row gap="var(--sg-s-3)">
        {task.project ? (
          <span style={{ ...mono, ...muted, fontSize: 12 }}>
            {shortSha(task.sha) ? `${task.project}@${shortSha(task.sha)}` : task.project}
          </span>
        ) : null}
        {task.hop !== null ? <span style={{ ...muted, fontSize: 12 }}>{tr.residents.hop(task.hop)}</span> : null}
        {task.calls !== null ? <span style={{ ...muted, fontSize: 12 }}>{tr.residents.calls(task.calls)}</span> : null}
        {task.patch_bytes !== null ? (
          <span style={{ ...muted, fontSize: 12 }}>{tr.residents.patchSize(fmtBytes(task.patch_bytes))}</span>
        ) : null}
      </Row>
      {task.patch_action !== null ? (
        <Row gap="var(--sg-s-3)">
          <a href={hrefFor('approvals', [String(task.patch_action)])}>{tr.residents.patchAction(task.patch_action)}</a>
          <StatusChip tone={action ? actionStatusTone(String(action.status)) : 'idle'} title={action ? String(action.status) : undefined}>
            {action ? tr.agents.actionStatus[String(action.status).toUpperCase()] ?? String(action.status) : tr.common.unknown}
          </StatusChip>
        </Row>
      ) : null}
    </Card>
  );
}

/** The whole board: one column per status, every task as a card. Read-only. */
export function TaskBoard({ tasks }: { tasks: ResidentTask[] }) {
  const columns = boardColumns(tasks);
  if (columns.length === 0) return <div style={{ ...muted, fontSize: 13 }}>{tr.residents.noTasks}</div>;
  return (
    <Grid min={260} gap="var(--sg-s-6)">
      {columns.map((col) => (
        <div key={col.key} style={{ display: 'grid', gap: 'var(--sg-s-4)', alignContent: 'start', minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>
            {col.label} <span style={{ ...muted, fontWeight: 400 }}>{tr.common.count(col.tasks.length)}</span>
          </div>
          <Stack gap="var(--sg-s-4)">
            {col.tasks.map((task) => (
              <TaskCard key={task.id} task={task} />
            ))}
          </Stack>
        </div>
      ))}
    </Grid>
  );
}

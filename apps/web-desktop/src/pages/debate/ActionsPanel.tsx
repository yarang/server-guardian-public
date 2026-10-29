import { useState } from 'react';
import type { FormEvent } from 'react';
import { Button, Panel, SegmentedControl, StatusChip, TextField } from '@server-guardian/ui';
import { listActions } from '../../api';
import { DataGate } from '../../components/DataGate';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Row, Stack, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { useApprovalsStore } from '../../state/approvalsStore';
import { THREAD_ID_RE, actionStatusTone, matchesActionFilter, riskLabel, riskTone, sortActionsByDecision } from '../../lib/debate';
import type { ActionFilter } from '../../lib/debate';
import type { ActionItem } from '../../types';
import { tr } from '../../i18n';

function ActionsList({ thread, project, filter, onOpen }: { thread: string; project: string; filter: ActionFilter; onOpen: (id: number) => void }) {
  const state = useHubData(() => listActions(thread || undefined, project || undefined));
  const { closedThreads } = useApprovalsStore();
  const columns: Column<ActionItem>[] = [
    {
      header: 'ID',
      mono: true,
      render: (a) => (
        <Button size="sm" variant="outline" aria-label={tr.debate.result.openAction(a.id)} onClick={() => onOpen(a.id)}>
          {`#${a.id}`}
        </Button>
      ),
    },
    { header: tr.debate.result.colTool, mono: true, render: (a) => a.tool },
    { header: tr.debate.actions.colRisk, render: (a) => <StatusChip tone={riskTone(a.risk)} title={a.risk}>
          {riskLabel(a.risk)}
        </StatusChip> },
    { header: tr.debate.result.colStatus,
      render: (a) => (
        <StatusChip tone={actionStatusTone(a.status)} title={String(a.status)}>
          {tr.debate.actionStatus[String(a.status).toUpperCase()] ?? String(a.status)}
        </StatusChip>
      ) },
    {
      header: tr.debate.actions.colThread,
      mono: true,
      render: (a) => (
        <span>
          {a.thread_id}
          {closedThreads.has(a.thread_id) ? (
            <>
              {' '}
              <span title={tr.debate.actions.threadClosedTip}>
                <StatusChip tone="idle">{tr.debate.actions.threadClosed}</StatusChip>
              </span>
            </>
          ) : null}
        </span>
      ),
    },
    {
      header: tr.debate.actions.colReason,
      render: (a) => (
        <span style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }} title={a.reason}>
          {a.reason}
        </span>
      ),
    },
    { header: tr.debate.actions.colDecidedBy, mono: true, render: (a) => a.decided_by ?? '' },
  ];
  return (
    <DataGate state={state} what={tr.debate.actions.what}>
      {(all) => {
        const actions = sortActionsByDecision(all.filter((a) => matchesActionFilter(a, filter)));
        return (
          <Stack gap="var(--sg-s-3)">
            <div style={{ ...muted, fontSize: 12 }}>
              {filter !== 'all' ? tr.debate.actions.countOf(actions.length, all.length) : tr.debate.actions.count(actions.length)}
            </div>
            <DataTable label={tr.debate.actions.title} columns={columns} rows={actions} getKey={(a) => String(a.id)} empty={tr.debate.actions.empty} />
          </Stack>
        );
      }}
    </DataGate>
  );
}

const FILTER_OPTIONS: Array<{ value: ActionFilter; label: string }> = [
  { value: 'all', label: tr.debate.actions.filterAll },
  { value: 'awaiting', label: tr.debate.actions.filterAwaiting },
  { value: 'held', label: tr.debate.actions.filterHeld },
  { value: 'decided', label: tr.debate.actions.filterDecided },
];

/**
 * Judge actions across threads, optionally filtered to one thread and by status. The list is always
 * ordered PROPOSED, then HELD, then decided ones. Each (re)load remounts the list, so Apply filter and a
 * changed thread filter always refetch; `version` bumps it after a run / reject.
 */
export function ActionsPanel({ version, project, onOpen }: { version: number; project: string; onOpen: (id: number) => void }) {
  const [input, setInput] = useState('');
  const [applied, setApplied] = useState({ thread: '', n: 0 });
  const [filterError, setFilterError] = useState('');
  // Opens on "Awaiting decision" when the project has proposals to decide; the operator's own choice wins afterwards.
  const { waiting } = useApprovalsStore();
  const [chosen, setChosen] = useState<ActionFilter | null>(null);
  const status: ActionFilter = chosen ?? ((waiting ?? 0) > 0 ? 'awaiting' : 'all');

  const apply = (e?: FormEvent) => {
    e?.preventDefault();
    const thread = input.trim();
    if (thread && !THREAD_ID_RE.test(thread)) {
      setFilterError(tr.debate.actions.badThreadFilter);
      return;
    }
    setFilterError('');
    setApplied((a) => ({ thread, n: a.n + 1 }));
  };

  return (
    <Panel title={tr.debate.actions.title}>
      <Stack gap="var(--sg-s-6)">
        <form onSubmit={apply}>
          <Row style={{ alignItems: 'flex-end' }}>
            <TextField
              label={tr.debate.actions.filterThread}
              mono
              placeholder="thread_..."
              value={input}
              error={filterError || undefined}
              onChange={(e) => setInput(e.target.value)}
            />
            <Button type="submit" variant="outline">
              {tr.common.apply}
            </Button>
          </Row>
        </form>
        <SegmentedControl aria-label={tr.debate.actions.statusFilter} options={FILTER_OPTIONS} value={status} onChange={(v) => setChosen(v as ActionFilter)} />
        <ActionsList filter={status} key={`${applied.n}:${version}:${project}`} thread={applied.thread} project={project} onOpen={onOpen} />
      </Stack>
    </Panel>
  );
}

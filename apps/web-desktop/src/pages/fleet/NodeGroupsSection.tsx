import { tr } from '../../i18n';
import { useEffect, useState } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { DataGate } from '../../components/DataGate';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Notice, NoticeList } from '../../components/Notice';
import { Stack, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import { rulesText } from '../../lib/groups';
import { fleetHref, navigateFleet } from '../../router';
import type { NodeGroup, NodeGroupList, FleetNode } from '../../types';
import { GroupDialog } from './GroupDialog';

function definitionText(g: NodeGroup): string {
  const parts: string[] = [];
  if (g.include.length) parts.push(tr.fleet.groupSummary.picked(g.include.length));
  if (g.rules.length) parts.push(tr.fleet.groupSummary.rules(rulesText(g.rules)));
  if (g.exclude.length) parts.push(tr.fleet.groupSummary.excluded(g.exclude.length));
  return parts.join(' · ') || tr.fleet.groupsSection.empty;
}

/** User-defined node groups stored on the hub: list with member counts, create, edit, delete. */
export function NodeGroupsSection({
  state,
  servers,
  onChanged,
  editId,
}: {
  state: HubDataState<NodeGroupList>;
  servers: FleetNode[];
  onChanged: () => void;
  /** `?edit=<id>` of the route: opens that group's dialog; closing clears it from the URL. */
  editId?: string;
}) {
  const [picked, setPicked] = useState<{ group: NodeGroup | null } | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  // The id whose dialog was closed; the URL clears a moment later, so it must not reopen meanwhile.
  const [dismissed, setDismissed] = useState<string | null>(null);
  useEffect(() => {
    if (!editId) setDismissed(null);
  }, [editId]);
  const count = state.data?.groups.length;

  const linked = editId && dismissed !== editId ? state.data?.groups.find((g) => g.id === editId) ?? null : null;
  const editing = picked ?? (linked ? { group: linked } : null);
  const setEditing = (next: { group: NodeGroup | null } | null) => {
    setPicked(next);
    if (next === null && editId) {
      setDismissed(editId);
      navigateFleet('groups');
    }
  };
  const missingLink = editId && state.data && !state.data.groups.some((g) => g.id === editId);

  const columns: Column<NodeGroup>[] = [
    {
      header: tr.fleet.groupsSection.columns.group,
      render: (g) => (
        <div style={{ display: 'grid', gap: 'var(--sg-s-1)' }}>
          <a href={fleetHref('nodes', [], { group: g.id })}>
            <strong>{g.name}</strong>
          </a>
          {g.description ? <span style={{ ...muted, fontSize: 12 }}>{g.description}</span> : null}
        </div>
      ),
    },
    {
      header: tr.fleet.groupsSection.columns.members,
      mono: true,
      render: (g) => <StatusChip tone={g.members.length ? 'info' : 'idle'}>{tr.fleet.groupsSection.nodesCount(g.members.length)}</StatusChip>,
    },
    { header: tr.fleet.groupsSection.columns.definition, render: (g) => <span style={{ fontSize: 12 }}>{definitionText(g)}</span> },
    {
      header: tr.fleet.groupsSection.columns.notes,
      render: (g) => (g.warnings.length ? <NoticeList tone="warn" items={g.warnings} /> : '—'),
    },
    {
      header: tr.fleet.groupsSection.columns.actions,
      align: 'right',
      render: (g) => (
        <Button size="sm" variant="ghost" aria-label={tr.fleet.groupsSection.editAria(g.name)} onClick={() => setEditing({ group: g })}>
          {tr.fleet.groupsSection.edit}
        </Button>
      ),
    },
  ];

  return (
    <Panel
      title={tr.fleet.groupsSection.title(count)}
      actions={
        <Button size="sm" variant="secondary" disabled={!state.data} onClick={() => setEditing({ group: null })}>
          {tr.fleet.groupsSection.newGroup}
        </Button>
      }
    >
      <Stack gap="var(--sg-s-6)">
        {banner ? <Notice tone="ok">{banner}</Notice> : null}
        {missingLink ? <Notice tone="warn">{tr.fleet.groupsSection.noGroupWithId(editId)}</Notice> : null}
        <DataGate state={state} what={tr.fleet.groupsSection.what}>
          {(data) => (
            <Stack gap="var(--sg-s-5)">
              <NoticeList tone="warn" items={data.warnings} />
              <DataTable
                label={tr.fleet.groupsSection.tableLabel}
                columns={columns}
                rows={data.groups}
                getKey={(g) => g.id}
                empty={tr.fleet.groupsSection.emptyTable}
              />
              {data.ungrouped.length ? (
                <div style={{ ...muted, fontSize: 12 }}>{tr.fleet.groupsSection.ungrouped(data.ungrouped.length)}</div>
              ) : null}
            </Stack>
          )}
        </DataGate>
      </Stack>
      {editing && state.data ? (
        <GroupDialog
          key={editing.group?.id ?? 'new'}
          group={editing.group}
          servers={servers}
          fields={state.data.catalog?.fields ?? []}
          onClose={() => setEditing(null)}
          onSaved={(res) => {
            setBanner(tr.fleet.groupsSection.saved(res.id, res.members.length));
            setEditing(null);
            onChanged();
          }}
          onDeleted={(id) => {
            setBanner(tr.fleet.groupsSection.deleted(id));
            setEditing(null);
            onChanged();
          }}
        />
      ) : null}
    </Panel>
  );
}

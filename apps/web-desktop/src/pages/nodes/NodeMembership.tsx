import { tr } from '../../i18n';
import { useState } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, saveGroup } from '../../api';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { ErrorNotice } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { Stack, muted } from '../../components/layout';
import { errorText } from '../../lib/format';
import { membershipOrigin, planRemove } from '../../lib/groupEdit';
import { fleetHref } from '../../router';
import type { HubStore } from '../../state/hubStore';
import type { NodeGroup, FleetNode } from '../../types';
import { AddToGroupDialog } from '../fleet/AddToGroupDialog';

/**
 * Group memberships of one node with how each comes about ("added": picked explicitly; "by rule":
 * matched by a rule) and the edits: add to a group, remove from one. Removing an explicit member
 * leaves `include`; removing a rule match adds the node to the group's `exclude`. Every save goes
 * through a confirm step and then reloads the shared groups.
 */
export function NodeMembership({ node, groups }: { node: FleetNode; groups: HubStore['groups'] }) {
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<NodeGroup | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const all = groups.data?.groups ?? [];
  const memberOf = all.filter((g) => g.members.includes(node.id));
  const plan = removing ? planRemove(removing, node.id) : null;

  const confirmRemove = async () => {
    if (!removing || !plan) return;
    setBusy(true);
    setError(null);
    try {
      const res = await saveGroup(plan.data);
      groups.reload();
      setBanner(
        res.members.includes(node.id)
          ? tr.fleet.membership.removedStill(removing.name, node.name)
          : tr.fleet.membership.removed(removing.name, node.name)
      );
      setRemoving(null);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<NodeGroup>[] = [
    {
      header: tr.fleet.membership.columns.group,
      render: (g) => (
        <a href={fleetHref('nodes', [], { group: g.id })}>
          <strong>{g.name}</strong>
        </a>
      ),
    },
    {
      header: tr.fleet.membership.columns.how,
      render: (g) =>
        membershipOrigin(g, node.id) === 'added' ? <StatusChip tone="info">{tr.fleet.membership.howAdded}</StatusChip> : <StatusChip tone="idle">{tr.fleet.membership.howRule}</StatusChip>,
    },
    {
      header: tr.fleet.membership.columns.actions,
      align: 'right',
      render: (g) => (
        <Button size="sm" variant="ghost" aria-label={tr.fleet.membership.removeAria(g.name)} onClick={() => setRemoving(g)}>
          {tr.fleet.membership.remove}
        </Button>
      ),
    },
  ];

  return (
    <Panel
      title={tr.fleet.membership.title}
      actions={
        <Button size="sm" variant="outline" disabled={all.length === 0} onClick={() => setAdding(true)}>
          {tr.fleet.membership.addToGroup}
        </Button>
      }
    >
      <Stack gap="var(--sg-s-5)">
        {banner ? <Notice tone="ok">{banner}</Notice> : null}
        {groups.error !== null ? (
          <ErrorNotice title={tr.fleet.membership.loadFailed} message={groups.error} onRetry={groups.reload} retrying={groups.loading} />
        ) : groups.data === null ? (
          <div style={muted}>{tr.fleet.membership.loading}</div>
        ) : (
          <DataTable label={tr.fleet.membership.tableLabel} columns={columns} rows={memberOf} getKey={(g) => g.id} empty={tr.fleet.membership.empty} />
        )}
      </Stack>
      {adding ? (
        <AddToGroupDialog
          groups={all}
          ids={[node.id]}
          nameOf={(id) => (id === node.id ? node.name : id)}
          onClose={() => setAdding(false)}
          onSaved={(g, res) => {
            setAdding(false);
            groups.reload();
            setBanner(
              res.members.includes(node.id) ? tr.fleet.membership.added(g.name, node.name) : tr.fleet.membership.notMember(g.name, node.name)
            );
          }}
        />
      ) : null}
      {removing && plan ? (
        <ConfirmModal
          title={tr.fleet.membership.confirmTitle}
          message={
            plan.how === 'include'
              ? tr.fleet.membership.confirmInclude(node.name, removing.name)
              : tr.fleet.membership.confirmRule(node.name, removing.name)
          }
          confirmLabel={tr.fleet.membership.confirmLabel}
          busy={busy}
          error={error}
          onConfirm={() => void confirmRemove()}
          onCancel={() => {
            setRemoving(null);
            setError(null);
          }}
        />
      ) : null}
    </Panel>
  );
}

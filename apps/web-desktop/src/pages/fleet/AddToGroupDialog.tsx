import { tr } from '../../i18n';
import { useState } from 'react';
import { Button } from '@server-guardian/ui';
import { AuthRequiredError, saveGroup } from '../../api';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { Stack } from '../../components/layout';
import { errorText } from '../../lib/format';
import { planAdd } from '../../lib/groupEdit';
import type { GroupSaveResult, NodeGroup } from '../../types';

const NONE = '';

/**
 * Adds nodes to an existing group: pick the group, read what will change, confirm. The picked ids
 * go into the group's `include` (deduplicated) and leave its `exclude`. A hub validation error is
 * shown as returned.
 */
export function AddToGroupDialog({
  groups,
  ids,
  nameOf,
  onClose,
  onSaved,
}: {
  groups: NodeGroup[];
  /** Nodes to add. */
  ids: string[];
  nameOf: (id: string) => string;
  onClose: () => void;
  onSaved: (group: NodeGroup, result: GroupSaveResult) => void;
}) {
  const [groupId, setGroupId] = useState(NONE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const group = groups.find((g) => g.id === groupId) ?? null;
  const plan = group ? planAdd(group, ids) : null;
  const list = (xs: string[]) => xs.map(nameOf).join(', ');

  const save = async () => {
    if (!group || !plan) return;
    setBusy(true);
    setError(null);
    try {
      onSaved(group, await saveGroup(plan.data));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <Modal title={tr.fleet.addToGroup.title} onClose={busy ? undefined : onClose}>
      <Stack gap="var(--sg-s-6)">
        <SelectField
          label={tr.fleet.addToGroup.group}
          value={groupId}
          options={[{ value: NONE, label: tr.fleet.addToGroup.choose }, ...groups.map((g) => ({ value: g.id, label: g.name }))]}
          onChange={(e) => {
            setGroupId(e.target.value);
            setError(null);
          }}
        />
        {group && plan ? (
          <section aria-label={tr.fleet.addToGroup.whatChanges} style={{ display: 'grid', gap: 'var(--sg-s-3)', fontSize: 13, lineHeight: 1.5, overflowWrap: 'anywhere' }}>
            {plan.added.length ? <div>{tr.fleet.addToGroup.added(group.name, list(plan.added))}</div> : null}
            {plan.unchanged.length ? <div>{tr.fleet.addToGroup.unchanged(list(plan.unchanged))}</div> : null}
            {plan.unexcluded.length ? (
              <div>{tr.fleet.addToGroup.unexcluded(group.name, list(plan.unexcluded))}</div>
            ) : null}
            {!plan.changes ? <div>{tr.fleet.addToGroup.nothing}</div> : null}
          </section>
        ) : null}
        {error ? <Notice tone="crit">{error}</Notice> : null}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--sg-s-3)', flexWrap: 'wrap' }}>
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            {tr.common.cancel}
          </Button>
          <Button variant="primary" loading={busy} disabled={!plan?.changes} onClick={() => void save()}>
            {tr.fleet.addToGroup.add}
          </Button>
        </div>
      </Stack>
    </Modal>
  );
}

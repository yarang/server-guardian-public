import { tr } from '../../i18n';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button, StatusChip, TextField } from '@server-guardian/ui';
import { AuthRequiredError, deleteGroup, previewGroup, saveGroup } from '../../api';
import { Card, Caption } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Modal } from '../../components/Modal';
import { Notice, NoticeList } from '../../components/Notice';
import { SelectField } from '../../components/SelectField';
import { Row, Stack, mono, muted } from '../../components/layout';
import { errorText } from '../../lib/format';
import { OP_LABELS } from '../../lib/groups';
import type {
  GroupCatalogField,
  GroupCondition,
  GroupField,
  GroupPreview,
  GroupRule,
  GroupSaveResult,
  NodeGroup,
  NodeGroupData,
  FleetNode,
} from '../../types';

/** Used when the hub sent no catalog (no servers.json); the hub validates fields and operators anyway. */
const FALLBACK_FIELDS: GroupCatalogField[] = [
  { field: 'tier', kind: 'scalar', ops: ['eq', 'ne'], values: [] },
  { field: 'arch', kind: 'scalar', ops: ['eq', 'ne'], values: [] },
  { field: 'guardian_role', kind: 'scalar', ops: ['eq', 'ne'], values: [] },
  { field: 'role', kind: 'scalar', ops: ['eq', 'ne'], values: [] },
  { field: 'tailscale_tags', kind: 'list', ops: ['contains', 'not_contains'], values: [] },
  { field: 'cli_support', kind: 'list', ops: ['contains', 'not_contains'], values: [] },
  { field: 'name', kind: 'scalar', ops: ['glob', 'eq', 'ne'], values: [] },
];

const PREVIEW_DELAY_MS = 300;

function newCondition(fields: GroupCatalogField[]): GroupCondition {
  const f = fields[0];
  return { field: f.field, op: f.ops[0], value: f.values[0] ?? '' };
}

function toggle(list: string[], id: string, on: boolean): string[] {
  return on ? (list.includes(id) ? list : [...list, id]) : list.filter((x) => x !== id);
}

function ConditionRow({
  condition,
  fields,
  label,
  onChange,
  onRemove,
}: {
  condition: GroupCondition;
  fields: GroupCatalogField[];
  label: string;
  onChange: (c: GroupCondition) => void;
  onRemove: () => void;
}) {
  const listId = useId();
  const field = fields.find((f) => f.field === condition.field) ?? fields[0];
  return (
    <Row gap="var(--sg-s-4)" style={{ alignItems: 'flex-end' }}>
      <SelectField
        label={tr.fleet.groupDialog.fieldAria(label)}
        value={condition.field}
        options={fields.map((f) => ({ value: f.field, label: f.field }))}
        onChange={(e) => {
          const next = fields.find((f) => f.field === e.target.value) ?? fields[0];
          onChange({ field: next.field as GroupField, op: next.ops.includes(condition.op) ? condition.op : next.ops[0], value: '' });
        }}
      />
      <SelectField
        label={tr.fleet.groupDialog.operatorAria(label)}
        value={condition.op}
        options={field.ops.map((op) => ({ value: op, label: OP_LABELS[op] ?? op }))}
        onChange={(e) => onChange({ ...condition, op: e.target.value as GroupCondition['op'] })}
      />
      <div style={{ minWidth: 200, flex: 1 }}>
        <TextField
          label={tr.fleet.groupDialog.valueAria(label)}
          mono
          list={listId}
          placeholder={condition.op === 'glob' ? 'site-b-*' : field.values[0] ?? ''}
          value={condition.value}
          onChange={(e) => onChange({ ...condition, value: e.target.value })}
        />
        <datalist id={listId}>
          {field.values.map((v) => (
            <option key={v} value={v} />
          ))}
        </datalist>
      </div>
      <Button size="sm" variant="ghost" aria-label={tr.fleet.groupDialog.removeAria(label)} onClick={onRemove}>
        {tr.fleet.groupDialog.remove}
      </Button>
    </Row>
  );
}

/**
 * Create / edit / delete a node group: explicit members, rules (OR of AND-ed conditions) and
 * exclusions, with a live member preview resolved by the hub. The hub validates everything.
 */
export function GroupDialog({
  group,
  initialInclude,
  servers,
  fields: catalogFields,
  onClose,
  onSaved,
  onDeleted,
}: {
  group: NodeGroup | null;
  /** Picked servers a new group starts with (ignored when editing). */
  initialInclude?: string[];
  servers: FleetNode[];
  fields: GroupCatalogField[];
  onClose: () => void;
  onSaved: (result: GroupSaveResult) => void;
  onDeleted: (id: string) => void;
}) {
  const fields = catalogFields.length ? catalogFields : FALLBACK_FIELDS;
  const isNew = group === null;
  const [name, setName] = useState(group?.name ?? '');
  const [description, setDescription] = useState(group?.description ?? '');
  const [include, setInclude] = useState<string[]>(group?.include ?? initialInclude ?? []);
  const [exclude, setExclude] = useState<string[]>(group?.exclude ?? []);
  const [rules, setRules] = useState<GroupRule[]>(group?.rules ?? []);
  const [search, setSearch] = useState('');
  const [preview, setPreview] = useState<GroupPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const previewSeq = useRef(0);

  // Live membership from the hub; a newer edit discards older answers.
  useEffect(() => {
    const seq = ++previewSeq.current;
    const timer = window.setTimeout(() => {
      previewGroup({ include, rules, exclude })
        .then((p) => {
          if (seq !== previewSeq.current) return;
          setPreview(p);
          setPreviewError(null);
        })
        .catch((err) => {
          if (seq !== previewSeq.current || err instanceof AuthRequiredError) return;
          setPreview(null);
          setPreviewError(errorText(err));
        });
    }, PREVIEW_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [include, rules, exclude]);

  const members = useMemo(() => new Set(preview?.members ?? []), [preview]);
  const nameOf = useMemo(() => new Map(servers.map((s) => [s.id, s.name])), [servers]);
  const q = search.trim().toLowerCase();
  const visible = q
    ? servers.filter((s) => [s.id, s.name, s.hostname].some((v) => (v ?? '').toLowerCase().includes(q)))
    : servers;

  const setRule = (i: number, rule: GroupRule | null) =>
    setRules((rs) => (rule === null ? rs.filter((_, j) => j !== i) : rs.map((r, j) => (j === i ? rule : r))));

  const save = async () => {
    const data: NodeGroupData = {
      ...(group ? { id: group.id, tone: group.tone ?? undefined } : {}),
      name: name.trim(),
      description: description.trim() || undefined,
      include,
      rules: rules.map((r) => ({ conditions: r.conditions.map((c) => ({ ...c, value: c.value.trim() })) })),
      exclude,
    };
    setBusy(true);
    setError(null);
    try {
      onSaved(await saveGroup(data));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!group) return;
    setBusy(true);
    setDeleteError(null);
    try {
      await deleteGroup(group.id);
      onDeleted(group.id);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setDeleteError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <>
      <Modal title={isNew ? tr.fleet.groupDialog.titleNew : tr.fleet.groupDialog.titleEdit(group.name)} width={760} onClose={busy ? undefined : onClose}>
        <Stack gap="var(--sg-s-6)">
          <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>
            {tr.fleet.groupDialog.intro}
          </div>
          <TextField label={tr.fleet.groupDialog.name} value={name} onChange={(e) => setName(e.target.value)} />
          <TextField label={tr.fleet.groupDialog.description} value={description} onChange={(e) => setDescription(e.target.value)} />

          <Stack gap="var(--sg-s-4)">
            <Caption>{tr.fleet.groupDialog.rulesCaption}</Caption>
            {rules.length === 0 ? <div style={{ ...muted, fontSize: 13 }}>{tr.fleet.groupDialog.noRules}</div> : null}
            {rules.map((rule, i) => (
              <Card key={i}>
                <Row style={{ justifyContent: 'space-between' }}>
                  <strong>{tr.fleet.groupDialog.rule(i + 1)}</strong>
                  <Button size="sm" variant="ghost" aria-label={tr.fleet.groupDialog.removeRuleAria(i + 1)} onClick={() => setRule(i, null)}>
                    {tr.fleet.groupDialog.removeRule}
                  </Button>
                </Row>
                {rule.conditions.map((c, j) => (
                  <ConditionRow
                    key={j}
                    condition={c}
                    fields={fields}
                    label={tr.fleet.groupDialog.conditionLabel(i + 1, j + 1)}
                    onChange={(next) => setRule(i, { conditions: rule.conditions.map((x, k) => (k === j ? next : x)) })}
                    onRemove={() =>
                      setRule(i, rule.conditions.length > 1 ? { conditions: rule.conditions.filter((_, k) => k !== j) } : null)
                    }
                  />
                ))}
                <div>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setRule(i, { conditions: [...rule.conditions, newCondition(fields)] })}
                  >
                    {tr.fleet.groupDialog.addCondition}
                  </Button>
                </div>
              </Card>
            ))}
            <div>
              <Button size="sm" variant="secondary" onClick={() => setRules((rs) => [...rs, { conditions: [newCondition(fields)] }])}>
                {tr.fleet.groupDialog.addRule}
              </Button>
            </div>
          </Stack>

          <Stack gap="var(--sg-s-4)">
            <Caption>{tr.fleet.groupDialog.nodesCaption(include.length, exclude.length)}</Caption>
            <TextField label={tr.fleet.groupDialog.searchLabel} placeholder={tr.fleet.groupDialog.searchPlaceholder} value={search} onChange={(e) => setSearch(e.target.value)} />
            <div
              style={{
                display: 'grid',
                maxHeight: 260,
                overflowY: 'auto',
                border: '1px solid var(--sg-border-subtle)',
                borderRadius: 'var(--sg-r-md)',
              }}
            >
              {visible.length === 0 ? <div style={{ ...muted, padding: 'var(--sg-s-4)' }}>{tr.fleet.groupDialog.noMatch}</div> : null}
              {visible.map((s) => (
                <Row
                  key={s.id}
                  gap="var(--sg-s-5)"
                  style={{ padding: 'var(--sg-s-3) var(--sg-s-4)', borderBottom: '1px solid var(--sg-border-subtle)' }}
                >
                  <span style={{ flex: 1, minWidth: 160, overflowWrap: 'anywhere' }}>
                    <strong>{s.name}</strong> <span style={{ ...mono, ...muted, fontSize: 12 }}>{s.hostname}</span>
                  </span>
                  {members.has(s.id) ? <StatusChip tone="ok">{tr.fleet.groupDialog.member}</StatusChip> : null}
                  <CheckboxField
                    aria-label={tr.fleet.groupDialog.includeAria(s.name)}
                    checked={include.includes(s.id)}
                    onChange={(e) => setInclude((l) => toggle(l, s.id, e.target.checked))}
                  >
                    {tr.fleet.groupDialog.include}
                  </CheckboxField>
                  <CheckboxField
                    aria-label={tr.fleet.groupDialog.excludeAria(s.name)}
                    checked={exclude.includes(s.id)}
                    onChange={(e) => setExclude((l) => toggle(l, s.id, e.target.checked))}
                  >
                    {tr.fleet.groupDialog.exclude}
                  </CheckboxField>
                </Row>
              ))}
            </div>
          </Stack>

          <section aria-label={tr.fleet.groupDialog.previewAria} aria-live="polite" style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
            <Caption>{tr.fleet.groupDialog.membersNow(preview?.members.length)}</Caption>
            {previewError ? <Notice tone="warn">{tr.fleet.groupDialog.previewUnavailable(previewError)}</Notice> : null}
            {preview ? (
              <div style={{ ...mono, fontSize: 12, overflowWrap: 'anywhere' }}>
                {preview.members.length ? preview.members.map((id) => nameOf.get(id) ?? id).join(', ') : tr.fleet.groupDialog.noNodes}
              </div>
            ) : null}
            {preview ? <NoticeList tone="warn" items={preview.warnings} /> : null}
          </section>

          {error ? <Notice tone="crit">{error}</Notice> : null}
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
            <div>
              {!isNew ? (
                <Button variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  {tr.fleet.groupDialog.deleteGroup}
                </Button>
              ) : null}
            </div>
            <div style={{ display: 'flex', gap: 'var(--sg-s-4)' }}>
              <Button variant="ghost" disabled={busy} onClick={onClose}>
                {tr.common.cancel}
              </Button>
              <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={() => void save()}>
                {tr.fleet.groupDialog.saveGroup}
              </Button>
            </div>
          </div>
        </Stack>
      </Modal>
      {confirmDelete && group ? (
        <ConfirmModal
          title={tr.fleet.groupDialog.confirmTitle}
          message={tr.fleet.groupDialog.confirmMessage(group.name)}
          confirmLabel={tr.fleet.groupDialog.confirmLabel}
          busy={busy}
          error={deleteError}
          onConfirm={() => void remove()}
          onCancel={() => {
            setConfirmDelete(false);
            setDeleteError(null);
          }}
        />
      ) : null}
    </>
  );
}

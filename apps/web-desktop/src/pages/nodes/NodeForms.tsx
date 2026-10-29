import { tr } from '../../i18n';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { Button, CodeBlock, StatusChip, TextField } from '@server-guardian/ui';
import { AuthRequiredError, HubError, checkNode, fetchPackageRoles, probeNode } from '../../api';
import { Caption } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { MonoBlock } from '../../components/MonoBlock';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { errorText } from '../../lib/format';
import {
  MAX_LIST,
  validateOpsRole,
  validateToken,
  validateTag,
} from '../../lib/nodeForm';
import type { ConnectionErrors, ConnectionForm, MetadataErrors, MetadataForm } from '../../lib/nodeForm';
import type { GroupCatalogField, HostKey, NodeCheckResult, NodeConnection, NodeProbe, PackageRoleInfo } from '../../types';

/** A failed hub call split into what the forms need: text, the request field it is about, a hint, a code. */
export interface FormError {
  message: string;
  code?: string;
  field?: string;
  hint?: string;
  keys?: HostKey[];
}

/** Null for an AuthRequiredError (the login dialog opens instead). */
export function toFormError(err: unknown): FormError | null {
  if (err instanceof AuthRequiredError) return null;
  if (err instanceof HubError) return { message: err.message, code: err.code, field: err.field, hint: err.hint, keys: err.keys };
  return { message: errorText(err) };
}

/** Command that prints the fingerprint to compare on the node. */
export const VERIFY_COMMAND = 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub';

/** Values the group rule catalog has seen for a field (datalist suggestions); free text stays allowed. */
export function catalogValues(fields: GroupCatalogField[], field: string): string[] {
  return fields.find((f) => f.field === field)?.values ?? [];
}

// --- Host key check ---------------------------------------------------------------

/** What a check was for: a later edit of any of these makes its result stale. */
export function checkSignature(c: ConnectionForm): string {
  return [c.id.trim(), c.hostname.trim(), c.port.trim()].join('|');
}

export interface HostKeyCheck {
  /** Signature of the connection the shown result was fetched for, or null before a check. */
  sig: string | null;
  result: NodeCheckResult | null;
  running: boolean;
  error: FormError | null;
  /** Fingerprint picked from the result (the operator's choice). */
  selected: string | null;
  /** "I compared this fingerprint with the server's". */
  confirmed: boolean;
  /** Runs the check; resolves to the failure (also kept in `error`) or null. */
  run: (conn: ConnectionForm) => Promise<FormError | null>;
  select: (fingerprint: string) => void;
  setConfirmed: (on: boolean) => void;
  /** Replace the shown keys (the hub's `fingerprint_mismatch` response carries the current ones). */
  adopt: (conn: ConnectionForm, keys: HostKey[]) => void;
  clear: () => void;
}

/** State of POST /api/nodes/check and of the operator's confirmation, shared by the wizard and the edit dialog. */
export function useHostKeyCheck(): HostKeyCheck {
  const [sig, setSig] = useState<string | null>(null);
  const [result, setResult] = useState<NodeCheckResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<FormError | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const seq = useRef(0);

  const clear = useCallback(() => {
    seq.current++;
    setSig(null);
    setResult(null);
    setError(null);
    setSelected(null);
    setConfirmed(false);
    setRunning(false);
  }, []);

  const run = useCallback(async (conn: ConnectionForm): Promise<FormError | null> => {
    const mine = ++seq.current;
    const body: NodeConnection = { id: conn.id.trim(), hostname: conn.hostname.trim(), user: conn.user.trim(), port: Number(conn.port.trim()) };
    setRunning(true);
    setError(null);
    try {
      const res = await checkNode(body);
      if (mine !== seq.current) return null;
      setResult(res);
      setSig(checkSignature(conn));
      setSelected(null);
      setConfirmed(false);
      return null;
    } catch (err) {
      if (mine !== seq.current) return null;
      const fe = toFormError(err);
      setResult(null);
      setSig(null);
      setSelected(null);
      setConfirmed(false);
      setError(fe);
      return fe;
    } finally {
      if (mine === seq.current) setRunning(false);
    }
  }, []);

  const adopt = useCallback((conn: ConnectionForm, keys: HostKey[]) => {
    seq.current++;
    setResult({ ok: true, id: conn.id.trim(), hostname: conn.hostname.trim(), port: Number(conn.port.trim()), id_exists: false, keys });
    setSig(checkSignature(conn));
    setSelected(null);
    setConfirmed(false);
    setRunning(false);
  }, []);

  const select = useCallback((fp: string) => {
    setSelected(fp);
    setConfirmed(false);
  }, []);

  return { sig, result, running, error, selected, confirmed, run, select, setConfirmed, adopt, clear };
}

/** The confirmed fingerprint for this connection, or null while there is no fresh check or no confirmation. */
export function confirmedFingerprint(check: HostKeyCheck, conn: ConnectionForm): string | null {
  return check.sig === checkSignature(conn) && check.selected !== null && check.confirmed ? check.selected : null;
}

/**
 * Check button, the host keys the node presented and the operator's confirmation. A result for an
 * older hostname or port is not shown (it would pin a key for the wrong destination).
 */
export function HostKeyPanel({
  check,
  conn,
  canCheck,
  onCheck,
  idMustBeNew,
  checkLabel = tr.fleet.forms.hostKey.check,
  fieldError,
}: {
  check: HostKeyCheck;
  conn: ConnectionForm;
  canCheck: boolean;
  /** Starts the check (the parent maps a failure's `field` onto its inputs). */
  onCheck: () => void;
  /** Registration: an id that already exists blocks the step. */
  idMustBeNew?: boolean;
  checkLabel?: string;
  /** The hub's `host_key_fingerprint` field error, shown with the check because it has no input of its own. */
  fieldError?: string;
}) {
  const fresh = check.result !== null && check.sig === checkSignature(conn);
  const result = fresh ? check.result : null;
  const name = useId();
  return (
    <Stack gap="var(--sg-s-5)">
      <Row>
        <Button variant="outline" loading={check.running} disabled={!canCheck || check.running} onClick={onCheck}>
          {checkLabel}
        </Button>
        <span style={{ ...muted, fontSize: 12 }}>{tr.fleet.forms.hostKey.scanHint}</span>
      </Row>
      {check.error && !check.error.field ? (
        <Notice tone="crit">
          {check.error.message}
          {check.error.hint ? `\n${check.error.hint}` : ''}
        </Notice>
      ) : null}
      {fieldError ? <Notice tone="crit">{fieldError}</Notice> : null}
      {result ? (
        <Stack gap="var(--sg-s-4)">
          {idMustBeNew && result.id_exists ? <Notice tone="crit">{tr.fleet.forms.hostKey.idExists(result.id)}</Notice> : null}
          <Caption>{tr.fleet.forms.hostKey.keysCaption}</Caption>
          <div role="radiogroup" aria-label={tr.fleet.forms.hostKey.keysAria} style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
            {result.keys.map((k) => (
              <label key={k.fingerprint} style={{ display: 'flex', gap: 'var(--sg-s-3)', alignItems: 'flex-start', cursor: 'pointer' }}>
                <input
                  type="radio"
                  name={name}
                  checked={check.selected === k.fingerprint}
                  onChange={() => check.select(k.fingerprint)}
                  style={{ marginTop: 3, accentColor: 'var(--sg-accent)' }}
                />
                <span style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                  <StatusChip tone="idle">{k.type}</StatusChip> <span style={mono}>{k.fingerprint}</span>
                </span>
              </label>
            ))}
          </div>
          <div style={{ ...muted, fontSize: 12, lineHeight: 1.5 }}>
            {tr.fleet.forms.hostKey.verifyPre}
            <span style={mono}>{VERIFY_COMMAND}</span>
            {tr.fleet.forms.hostKey.verifyPost}
          </div>
          <CheckboxField checked={check.confirmed} disabled={check.selected === null} onChange={(e) => check.setConfirmed(e.target.checked)}>
            {tr.fleet.forms.hostKey.confirm}
          </CheckboxField>
        </Stack>
      ) : check.result !== null ? (
        <Notice tone="warn">{tr.fleet.forms.hostKey.stale}</Notice>
      ) : null}
    </Stack>
  );
}

// --- Fields -----------------------------------------------------------------------

/** Text input followed by "Add"; entries show as removable chips. Enter or leaving the field also adds. */
export function ListField({
  label,
  items,
  onChange,
  validate,
  suggestions = [],
  placeholder,
  error,
}: {
  label: string;
  items: string[];
  onChange: (next: string[]) => void;
  validate: (value: string) => string | null;
  suggestions?: string[];
  placeholder?: string;
  error?: string;
}) {
  const [draft, setDraft] = useState('');
  const [draftError, setDraftError] = useState<string | null>(null);
  const listId = useId();

  const commit = (): void => {
    const value = draft.trim();
    if (value === '') {
      setDraftError(null);
      return;
    }
    const problem = validate(value) ?? (items.length >= MAX_LIST ? tr.fleet.validation.maxEntries(MAX_LIST) : null);
    if (problem) {
      setDraftError(problem);
      return;
    }
    if (!items.includes(value)) onChange([...items, value]);
    setDraft('');
    setDraftError(null);
  };

  const chip: CSSProperties = { display: 'inline-flex', alignItems: 'center', gap: 'var(--sg-s-2)' };
  return (
    <Stack gap="var(--sg-s-3)">
      <Row gap="var(--sg-s-4)" style={{ alignItems: 'flex-end' }}>
        <div style={{ minWidth: 200, flex: 1 }}>
          <TextField
            label={label}
            mono
            list={listId}
            placeholder={placeholder}
            value={draft}
            error={draftError ?? error}
            onChange={(e) => {
              setDraft(e.target.value);
              setDraftError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                commit();
              }
            }}
            onBlur={commit}
          />
          <datalist id={listId}>
            {suggestions.map((v) => (
              <option key={v} value={v} />
            ))}
          </datalist>
        </div>
        <Button size="sm" variant="outline" aria-label={tr.fleet.forms.list.addAria(label)} disabled={draft.trim() === ''} onClick={commit}>
          {tr.fleet.forms.list.add}
        </Button>
      </Row>
      {items.length > 0 ? (
        <Row gap="var(--sg-s-3)">
          {items.map((v) => (
            <span key={v} style={chip}>
              <StatusChip tone="idle">{v}</StatusChip>
              <Button size="sm" variant="ghost" aria-label={tr.fleet.forms.list.removeAria(label, v)} onClick={() => onChange(items.filter((x) => x !== v))}>
                {tr.fleet.forms.list.remove}
              </Button>
            </span>
          ))}
        </Row>
      ) : null}
    </Stack>
  );
}

/** Id (new nodes only), HostName, User and Port with inline errors (client checks, then the hub's `field` errors). */
export function ConnectionFields({
  value,
  onChange,
  errors,
  idLocked,
  disabled,
}: {
  value: ConnectionForm;
  onChange: (next: ConnectionForm) => void;
  errors: ConnectionErrors;
  idLocked?: boolean;
  disabled?: boolean;
}) {
  const set = (k: keyof ConnectionForm) => (e: { target: { value: string } }) => onChange({ ...value, [k]: e.target.value });
  return (
    <Stack gap="var(--sg-s-5)">
      <TextField
        label={tr.fleet.forms.connection.id}
        mono
        value={value.id}
        disabled={idLocked || disabled}
        hint={idLocked ? tr.fleet.forms.connection.idHintLocked : tr.fleet.forms.connection.idHint}
        error={errors.id}
        autoComplete="off"
        onChange={set('id')}
      />
      <TextField label={tr.fleet.forms.connection.hostName} mono value={value.hostname} disabled={disabled} placeholder={tr.fleet.forms.connection.hostPlaceholder} error={errors.hostname} autoComplete="off" onChange={set('hostname')} />
      <Row gap="var(--sg-s-5)" style={{ alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 160 }}>
          <TextField label={tr.fleet.forms.connection.user} mono value={value.user} disabled={disabled} error={errors.user} autoComplete="off" onChange={set('user')} />
        </div>
        <div style={{ width: 140 }}>
          <TextField label={tr.fleet.forms.connection.port} mono inputMode="numeric" value={value.port} disabled={disabled} error={errors.port} autoComplete="off" onChange={set('port')} />
        </div>
      </Row>
    </Stack>
  );
}

/** The roles file as the hub reads it; empty while loading or when the hub cannot read it (the field stays free text). */
function usePackageRoles(): PackageRoleInfo[] {
  const [roles, setRoles] = useState<PackageRoleInfo[]>([]);
  useEffect(() => {
    let live = true;
    fetchPackageRoles()
      .then((list) => live && setRoles(list.ok ? list.roles : []))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return roles;
}

/** A role name the roles file does not define: package actions on the node would be refused until it is fixed. */
function roleProblem(items: string[], known: PackageRoleInfo[]): string | undefined {
  if (known.length === 0) return undefined;
  const unknown = items.filter((r) => !known.some((k) => k.name === r));
  return unknown.length ? tr.fleet.validation.opsRoleUnknown(unknown.join(', ')) : undefined;
}

/** Tier, role, cli_support, tailscale_tags and description. `guardian_role` is fixed to node and not editable. */
export function MetadataFields({
  value,
  onChange,
  errors,
  fields,
}: {
  value: MetadataForm;
  onChange: (next: MetadataForm) => void;
  errors: MetadataErrors;
  /** The group rule catalog: its values are offered as suggestions. */
  fields: GroupCatalogField[];
}) {
  const tierList = useId();
  const roleList = useId();
  const packageRoles = usePackageRoles();
  return (
    <Stack gap="var(--sg-s-5)">
      <div>
        <TextField
          label={tr.fleet.forms.metadata.tier}
          mono
          list={tierList}
          value={value.tier}
          error={errors.tier}
          placeholder="Tier-2"
          autoComplete="off"
          onChange={(e) => onChange({ ...value, tier: e.target.value })}
        />
        <datalist id={tierList}>
          {catalogValues(fields, 'tier').map((v) => (
            <option key={v} value={v} />
          ))}
        </datalist>
      </div>
      <div>
        <TextField
          label={tr.fleet.forms.metadata.role}
          mono
          list={roleList}
          value={value.role}
          error={errors.role}
          placeholder="ssh_target_node"
          autoComplete="off"
          onChange={(e) => onChange({ ...value, role: e.target.value })}
        />
        <datalist id={roleList}>
          {catalogValues(fields, 'role').map((v) => (
            <option key={v} value={v} />
          ))}
        </datalist>
      </div>
      <TextField label={tr.fleet.forms.metadata.guardianRole} mono value="node" readOnly disabled hint={tr.fleet.forms.metadata.guardianRoleHint} />
      <ListField
        label={tr.fleet.forms.metadata.cliSupport}
        items={value.cli_support}
        onChange={(cli_support) => onChange({ ...value, cli_support })}
        validate={validateToken}
        suggestions={catalogValues(fields, 'cli_support')}
        placeholder="ssh_agent_remote_exec"
        error={errors.cli_support}
      />
      <ListField
        label={tr.fleet.forms.metadata.opsRoles}
        items={value.ops_roles}
        onChange={(ops_roles) => onChange({ ...value, ops_roles })}
        validate={validateOpsRole}
        suggestions={packageRoles.map((r) => r.name)}
        placeholder="llm_cli_host"
        error={errors.ops_roles ?? roleProblem(value.ops_roles, packageRoles)}
      />
      <Caption>{tr.fleet.forms.metadata.opsRolesHint}</Caption>
      <ListField
        label={tr.fleet.forms.metadata.tailscaleTags}
        items={value.tailscale_tags}
        onChange={(tailscale_tags) => onChange({ ...value, tailscale_tags })}
        validate={validateTag}
        suggestions={catalogValues(fields, 'tailscale_tags')}
        placeholder="tag:fleet"
        error={errors.tailscale_tags}
      />
      <TextField
        label={tr.fleet.forms.metadata.description}
        value={value.description}
        error={errors.description}
        hint={tr.fleet.forms.metadata.descriptionHint}
        onChange={(e) => onChange({ ...value, description: e.target.value })}
      />
    </Stack>
  );
}

// --- Probe result -----------------------------------------------------------------

/**
 * Result of `ssh <id> uname -m`: ok with the arch, the key line to install, or the stderr tail.
 * Everything shown comes from the hub's response.
 */
export function ProbeOutcome({
  nodeId,
  probe,
  authorizedKey,
  keyBlocker,
  testing,
  testError,
  onTestAgain,
}: {
  nodeId: string;
  probe: NodeProbe;
  authorizedKey?: string;
  /** Why the key line is empty (the hub's address is unknown). */
  keyBlocker?: string;
  testing: boolean;
  testError: string | null;
  onTestAgain: () => void;
}) {
  const again = (
    <div>
      <Button variant="outline" size="sm" loading={testing} onClick={onTestAgain}>
        {tr.fleet.forms.probe.testAgain}
      </Button>
    </div>
  );
  return (
    <Stack gap="var(--sg-s-4)">
      {probe.status === 'ok' ? (
        <Row>
          <StatusChip tone="ok">{tr.fleet.forms.probe.connected}</StatusChip>
          <span style={mono}>{`${probe.arch ? `${tr.fleet.forms.probe.arch(probe.arch)} · ` : ''}${probe.duration_ms} ms`}</span>
          {probe.sudo ? (
            <StatusChip tone={probe.sudo === 'yes' ? 'ok' : 'warn'} title={probe.sudo === 'yes' ? undefined : tr.fleet.forms.probe.sudoNoHint}>
              {probe.sudo === 'yes' ? tr.fleet.forms.probe.sudoYes : tr.fleet.forms.probe.sudoNo}
            </StatusChip>
          ) : null}
        </Row>
      ) : probe.status === 'key_not_installed' ? (
        <>
          <Notice tone="warn">
            {tr.fleet.forms.probe.keyNotInstalled}
          </Notice>
          {authorizedKey ? <CodeBlock title={tr.fleet.forms.probe.authorizedKeysLine} code={authorizedKey} /> : <Notice tone="warn">{keyBlocker || tr.fleet.forms.probe.noKeyLine}</Notice>}
          <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>
            {tr.fleet.forms.probe.adminMacPre}
            <span style={mono}>{`scripts/setup-hub-ssh.sh ${nodeId}`}</span>
            {tr.fleet.forms.probe.adminMacPost}
          </div>
          {probe.stderr ? <MonoBlock text={probe.stderr} maxHeight={120} /> : null}
          {again}
        </>
      ) : (
        <>
          <Notice tone="crit">{tr.fleet.forms.probe.unreachable}</Notice>
          {probe.stderr ? <MonoBlock text={probe.stderr} maxHeight={120} tone="crit" /> : null}
          {again}
        </>
      )}
      {testError ? <Notice tone="crit">{testError}</Notice> : null}
    </Stack>
  );
}

/** Probe result of a node plus the "Test again" call (POST /api/nodes/{id}/probe). */
export interface NodeProbeState {
  probe: NodeProbe | null;
  authorizedKey: string | undefined;
  keyBlocker: string | undefined;
  testing: boolean;
  error: string | null;
  /** Show a probe that came with a register or update response. */
  show: (probe: NodeProbe | undefined, authorizedKey?: string, keyBlocker?: string) => void;
  /** Run the probe on the hub; `onDone` fires after a successful call (the arch may have changed). */
  test: (id: string, onDone?: () => void) => Promise<void>;
  reset: () => void;
}

export function useNodeProbe(): NodeProbeState {
  const [probe, setProbe] = useState<NodeProbe | null>(null);
  const [authorizedKey, setKey] = useState<string | undefined>(undefined);
  const [keyBlocker, setBlocker] = useState<string | undefined>(undefined);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const show = useCallback((p: NodeProbe | undefined, key?: string, blocker?: string) => {
    setProbe(p ?? null);
    setKey(key);
    setBlocker(blocker);
    setError(null);
  }, []);

  const test = useCallback(async (id: string, onDone?: () => void) => {
    setTesting(true);
    setError(null);
    try {
      const res = await probeNode(id);
      if (!alive.current) return;
      setProbe(res.probe);
      setKey(res.authorized_key);
      setBlocker(res.authorized_key_blocker);
      onDone?.();
    } catch (err) {
      const fe = toFormError(err);
      if (alive.current && fe) setError(fe.message);
    } finally {
      if (alive.current) setTesting(false);
    }
  }, []);

  const reset = useCallback(() => {
    setProbe(null);
    setKey(undefined);
    setBlocker(undefined);
    setError(null);
  }, []);

  return { probe, authorizedKey, keyBlocker, testing, error, show, test, reset };
}

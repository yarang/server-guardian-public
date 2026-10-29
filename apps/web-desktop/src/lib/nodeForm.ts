import { tr } from '../i18n';
import type { NodeMetadata, NodeUpdateRequest, FleetNode } from '../types';

/**
 * Client-side checks that mirror the hub's validation (docs/design/node-registration.md, sections 3
 * and 8) so obvious mistakes show before a request. The hub stays authoritative: every function
 * returns an error message or null, and a hub `field` error always wins. The patterns and limits
 * are the ones in apps/agent/nodes_validate.go; keep the two in step. Values are trimmed first, like the hub does.
 */

const ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
const LABEL = '[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?';
const DNS_RE = new RegExp(`^${LABEL}(\\.${LABEL})*$`);
const IPV4_RE = /^(0|[1-9]\d{0,2})(\.(0|[1-9]\d{0,2})){3}$/;
const USER_RE = /^[a-z_][a-z0-9_-]{0,31}$/;
const TIER_RE = /^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,31}$/;
const TOKEN_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const ROLE_RE = /^[a-z][a-z0-9_]{0,31}$/;
const TAG_RE = /^tag:[a-z0-9][a-z0-9-]{0,62}$/;

export const MAX_LIST = 16;
export const MAX_DESCRIPTION = 500;
export const DEFAULT_USER = 'ubuntu';
export const DEFAULT_PORT = '22';

/** Words the API paths and dashboard routes use next to a node id (`#/nodes/new`, `/api/nodes/check`); the hub refuses them too. */
export const RESERVED_IDS = ['new', 'registration', 'check', 'probe'];

export function validateId(value: string): string | null {
  if (!ID_RE.test(value)) return tr.fleet.validation.id;
  if (RESERVED_IDS.includes(value)) return tr.fleet.validation.idReserved(value);
  return null;
}

export function validateHostname(value: string): string | null {
  if (value === '') return tr.fleet.validation.hostEmpty;
  if (value.length > 253) return tr.fleet.validation.hostTooLong;
  if (IPV4_RE.test(value)) return value.split('.').every((o) => Number(o) <= 255) ? null : tr.fleet.validation.hostBadIpv4;
  const last = value.split('.').pop() ?? '';
  if (/^\d+$/.test(last)) return tr.fleet.validation.hostBadIpv4;
  return DNS_RE.test(value) ? null : tr.fleet.validation.hostFormat;
}

export function validateUser(value: string): string | null {
  return USER_RE.test(value) ? null : tr.fleet.validation.user;
}

export function validatePort(value: string): string | null {
  const n = Number(value);
  return /^\d+$/.test(value) && n >= 1 && n <= 65535 ? null : tr.fleet.validation.port;
}

export function validateTier(value: string): string | null {
  const v = value.trim();
  return v === '' || TIER_RE.test(v) ? null : tr.fleet.validation.tier;
}

export function validateToken(value: string): string | null {
  return TOKEN_RE.test(value.trim()) ? null : tr.fleet.validation.token;
}

export function validateOpsRole(value: string): string | null {
  return ROLE_RE.test(value.trim()) ? null : tr.fleet.validation.opsRole;
}

export function validateTag(value: string): string | null {
  return TAG_RE.test(value.trim()) ? null : tr.fleet.validation.tag;
}

export function validateDescription(value: string): string | null {
  const v = value.trim();
  if (/\p{Cc}/u.test(v)) return tr.fleet.validation.descControl;
  return Array.from(v).length <= MAX_DESCRIPTION ? null : tr.fleet.validation.descMax(MAX_DESCRIPTION);
}

/** Connection fields as typed (port stays a string until sent). */
export interface ConnectionForm {
  id: string;
  hostname: string;
  user: string;
  port: string;
}

export type ConnectionErrors = Partial<Record<keyof ConnectionForm, string>>;

/** Errors by field; `idLocked` skips the id (an existing node's id is not editable). */
export function validateConnection(c: ConnectionForm, idLocked = false): ConnectionErrors {
  const errors: ConnectionErrors = {};
  const checks: Array<[keyof ConnectionForm, string | null]> = [
    ['id', idLocked ? null : validateId(c.id)],
    ['hostname', validateHostname(c.hostname)],
    ['user', validateUser(c.user)],
    ['port', validatePort(c.port)],
  ];
  for (const [field, message] of checks) if (message) errors[field] = message;
  return errors;
}

/** Metadata as edited: lists are real arrays, everything else free text. */
export interface MetadataForm {
  tier: string;
  role: string;
  cli_support: string[];
  ops_roles: string[];
  tailscale_tags: string[];
  description: string;
}

export const EMPTY_METADATA: MetadataForm = { tier: '', role: '', cli_support: [], ops_roles: [], tailscale_tags: [], description: '' };

export type MetadataErrors = Partial<Record<'tier' | 'role' | 'cli_support' | 'ops_roles' | 'tailscale_tags' | 'description', string>>;

export function validateMetadata(m: MetadataForm): MetadataErrors {
  const errors: MetadataErrors = {};
  const tier = validateTier(m.tier);
  if (tier) errors.tier = tier;
  if (m.role.trim() !== '') {
    const role = validateToken(m.role);
    if (role) errors.role = role;
  }
  const listError = (items: string[], check: (v: string) => string | null): string | undefined =>
    items.length > MAX_LIST ? tr.fleet.validation.maxEntries(MAX_LIST) : items.map(check).find((e): e is string => e !== null) ?? undefined;
  const cli = listError(m.cli_support, validateToken);
  if (cli) errors.cli_support = cli;
  const roles = listError(m.ops_roles, validateOpsRole);
  if (roles) errors.ops_roles = roles;
  const tags = listError(m.tailscale_tags, validateTag);
  if (tags) errors.tailscale_tags = tags;
  const desc = validateDescription(m.description);
  if (desc) errors.description = desc;
  return errors;
}

/** Registration `metadata`: empty fields are left out, `guardian_role` is always `node`. */
export function metadataPayload(m: MetadataForm): NodeMetadata {
  const out: NodeMetadata = { guardian_role: 'node' };
  if (m.tier.trim()) out.tier = m.tier.trim();
  if (m.role.trim()) out.role = m.role.trim();
  if (m.cli_support.length) out.cli_support = m.cli_support;
  if (m.ops_roles.length) out.ops_roles = m.ops_roles;
  if (m.tailscale_tags.length) out.tailscale_tags = m.tailscale_tags;
  if (m.description.trim()) out.description = m.description.trim();
  return out;
}

/** The editable metadata of an inventory entry. */
export function metadataOf(node: FleetNode): MetadataForm {
  return {
    tier: node.tier ?? '',
    role: node.role ?? '',
    cli_support: node.cli_support ?? [],
    ops_roles: node.ops_roles ?? [],
    tailscale_tags: node.tailscale_tags ?? [],
    description: node.description ?? '',
  };
}

/**
 * The address the hub scans and pins for a node: the Tailscale IP of an inventory node when it is in
 * 100.64.x, else its hostname (the same rule as the hub); user defaults to ubuntu and port to 22.
 * A fleet file node's own values are used as they are.
 */
export function pinTarget(node: FleetNode): ConnectionForm {
  const viaTailscale = node.connection === 'inventory' && (node.tailscale_ip ?? '').startsWith('100.64.');
  return {
    id: node.id,
    hostname: viaTailscale ? (node.tailscale_ip as string) : node.hostname,
    user: node.user || DEFAULT_USER,
    port: String(node.port || Number(DEFAULT_PORT)),
  };
}

export function connectionOf(node: FleetNode): ConnectionForm {
  return { id: node.id, hostname: node.hostname, user: node.user, port: String(node.port) };
}

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((v, i) => v === b[i]);

/** True when applying `next` instead of `base` changes hostname or port (needs a fresh host key check). */
export function destinationChanged(base: ConnectionForm, next: ConnectionForm): boolean {
  return base.hostname !== next.hostname.trim() || base.port !== next.port.trim();
}

/** True when any of hostname, user or port differs. */
export function connectionChanged(base: ConnectionForm, next: ConnectionForm): boolean {
  return destinationChanged(base, next) || base.user !== next.user.trim();
}

/**
 * Builds a PUT body with only what changed. Cleared metadata becomes `null` (the hub removes the key).
 * `fingerprint` is attached when given. Returns null when nothing changed.
 */
export function updatePayload(
  baseConn: ConnectionForm,
  conn: ConnectionForm,
  baseMeta: MetadataForm,
  meta: MetadataForm,
  fingerprint: string | null
): NodeUpdateRequest | null {
  const body: NodeUpdateRequest = {};
  if (conn.hostname.trim() !== baseConn.hostname) body.hostname = conn.hostname.trim();
  if (conn.user.trim() !== baseConn.user) body.user = conn.user.trim();
  if (conn.port.trim() !== baseConn.port) body.port = Number(conn.port.trim());
  const metadata: NonNullable<NodeUpdateRequest['metadata']> = {};
  if (meta.tier.trim() !== baseMeta.tier) metadata.tier = meta.tier.trim() || null;
  if (meta.role.trim() !== baseMeta.role) metadata.role = meta.role.trim() || null;
  if (!sameList(meta.cli_support, baseMeta.cli_support)) metadata.cli_support = meta.cli_support.length ? meta.cli_support : null;
  if (!sameList(meta.ops_roles, baseMeta.ops_roles)) metadata.ops_roles = meta.ops_roles.length ? meta.ops_roles : null;
  if (!sameList(meta.tailscale_tags, baseMeta.tailscale_tags)) metadata.tailscale_tags = meta.tailscale_tags.length ? meta.tailscale_tags : null;
  if (meta.description.trim() !== baseMeta.description) metadata.description = meta.description.trim() || null;
  if (Object.keys(metadata).length) body.metadata = metadata;
  if (Object.keys(body).length === 0) return null;
  if (fingerprint && (body.hostname !== undefined || body.port !== undefined || body.user !== undefined)) body.host_key_fingerprint = fingerprint;
  return body;
}

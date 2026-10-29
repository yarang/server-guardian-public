import { describe, expect, it } from 'vitest';
import {
  EMPTY_METADATA,
  MAX_DESCRIPTION,
  MAX_LIST,
  validateDescription,
  validateHostname,
  validateId,
  validateMetadata,
  validatePort,
  validateTag,
  validateTier,
  validateToken,
  validateUser,
} from '../src/lib/nodeForm';
import { ko } from '../src/i18n/ko';

// These cases follow apps/agent/nodes_validate.go: what the hub accepts, the form accepts, and the reverse.
const ok = (v: string | null) => expect(v).toBeNull();
const bad = (v: string | null) => expect(v).not.toBeNull();

describe('node form validation mirrors the hub', () => {
  it('ids: lowercase, up to 63 characters, not reserved', () => {
    ok(validateId('site-a-arm1'));
    ok(validateId('a'.repeat(63)));
    bad(validateId('a'.repeat(64)));
    bad(validateId('-a'));
    bad(validateId('A1'));
    for (const word of ['new', 'registration', 'check', 'probe']) bad(validateId(word));
  });

  it('hostnames: IPv4 literal or DNS name, no numeric last label, at most 253 characters', () => {
    for (const h of ['10.0.0.1', 'host', 'a-b.example.com', '255.255.255.255']) ok(validateHostname(h));
    for (const h of ['', '999.1.1.1', '01.2.3.4', '1.2.3', 'a_b', '-host', 'host name', 'host:22', 'a.'.repeat(130) + 'com']) bad(validateHostname(h));
    bad(validateHostname('x'.repeat(64)));
  });

  it('users and ports', () => {
    ok(validateUser('ubuntu'));
    ok(validateUser('_svc-1'));
    bad(validateUser('Ubuntu'));
    bad(validateUser('1abc'));
    bad(validateUser('a'.repeat(33)));
    ok(validatePort('22'));
    ok(validatePort('65535'));
    bad(validatePort('0'));
    bad(validatePort('65536'));
    bad(validatePort('2x'));
  });

  it('tier starts with a letter or digit and is at most 32 characters; surrounding spaces are trimmed', () => {
    ok(validateTier(''));
    ok(validateTier('Tier-1'));
    ok(validateTier('  Tier 2  '));
    ok(validateTier('a'.repeat(32)));
    bad(validateTier('a'.repeat(33)));
    bad(validateTier('-tier'));
    bad(validateTier('.tier'));
    bad(validateTier('tier/1'));
  });

  it('ops_roles are lowercase role names starting with a letter', () => {
    expect(validateMetadata({ ...EMPTY_METADATA, ops_roles: ['web', 'llm_cli_host'] }).ops_roles).toBeUndefined();
    for (const bad of ['Web', '9db', 'a-b', 'x'.repeat(33)]) {
      expect(validateMetadata({ ...EMPTY_METADATA, ops_roles: [bad] }).ops_roles).toBe(ko.fleet.validation.opsRole);
    }
  });

  it('role and cli_support tokens: 1-64 characters, start alphanumeric', () => {
    ok(validateToken('ssh_agent_remote_exec'));
    ok(validateToken('a'.repeat(64)));
    bad(validateToken('a'.repeat(65)));
    bad(validateToken('_x'));
    bad(validateToken(''));
  });

  it('tags: tag:name with a lowercase alphanumeric start, name up to 63 characters', () => {
    ok(validateTag('tag:fleet'));
    ok(validateTag('tag:a-b-1'));
    ok(validateTag('tag:' + 'a'.repeat(63)));
    bad(validateTag('tag:' + 'a'.repeat(64)));
    bad(validateTag('tag:-x'));
    bad(validateTag('tag:'));
    bad(validateTag('tag:Fleet'));
    bad(validateTag('fleet'));
  });

  it('description: counts characters not UTF-16 units, no control characters', () => {
    ok(validateDescription(''));
    ok(validateDescription('x'.repeat(MAX_DESCRIPTION)));
    bad(validateDescription('x'.repeat(MAX_DESCRIPTION + 1)));
    // 500 astral characters are 1000 UTF-16 units but 500 characters.
    ok(validateDescription('\u{1F600}'.repeat(MAX_DESCRIPTION)));
    bad(validateDescription('a\nb'));
    bad(validateDescription('a\tb'));
    bad(validateDescription('a\u0000b'));
    ok(validateDescription('  padded  '));
  });

  it('metadata: checks the entries of the lists and the list sizes', () => {
    expect(validateMetadata(EMPTY_METADATA)).toEqual({});
    expect(validateMetadata({ ...EMPTY_METADATA, tailscale_tags: ['tag:fleet', 'nope'] }).tailscale_tags).toBeTruthy();
    expect(validateMetadata({ ...EMPTY_METADATA, cli_support: ['ok', '-bad'] }).cli_support).toBeTruthy();
    expect(validateMetadata({ ...EMPTY_METADATA, cli_support: Array.from({ length: MAX_LIST + 1 }, (_, i) => `c${i}`) }).cli_support).toBe(ko.fleet.validation.maxEntries(MAX_LIST));
    expect(validateMetadata({ ...EMPTY_METADATA, tier: '-x' }).tier).toBeTruthy();
    expect(validateMetadata({ ...EMPTY_METADATA, role: ' ' })).toEqual({});
    expect(validateMetadata({ ...EMPTY_METADATA, role: 'bad role' }).role).toBeTruthy();
  });
});

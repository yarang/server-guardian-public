import { describe, expect, it } from 'vitest';
import { ko } from '../src/i18n/ko';
import { isOnline, reachabilityInfo } from '../src/lib/reachability';

describe('reachability helper', () => {
  it('maps each hub classification to a tone and label', () => {
    expect(reachabilityInfo({ reachability: 'ok' })).toEqual({ tone: 'ok', label: ko.shell.reachability.ok });
    expect(reachabilityInfo({ reachability: 'command_failed' })).toEqual({ tone: 'warn', label: ko.shell.reachability.commandFailed });
    expect(reachabilityInfo({ reachability: 'auth_failed' })).toEqual({ tone: 'crit', label: ko.shell.reachability.authFailed });
    expect(reachabilityInfo({ reachability: 'host_key_changed' })).toEqual({ tone: 'crit', label: ko.shell.reachability.hostKeyChanged });
    expect(reachabilityInfo({ reachability: 'unreachable' })).toEqual({ tone: 'crit', label: ko.shell.reachability.unreachable });
  });

  it('prefers reachability over the old status', () => {
    expect(reachabilityInfo({ reachability: 'command_failed', status: 'OFFLINE' }).tone).toBe('warn');
    expect(isOnline({ reachability: 'command_failed', status: 'OFFLINE' })).toBe(true);
    expect(isOnline({ reachability: 'ok' })).toBe(true);
    for (const r of ['auth_failed', 'host_key_changed', 'unreachable'] as const) expect(isOnline({ reachability: r, status: 'ONLINE' })).toBe(false);
  });

  it('falls back to status for older hubs', () => {
    expect(isOnline({ status: 'ONLINE' })).toBe(true);
    expect(isOnline({ status: 'OFFLINE' })).toBe(false);
    expect(reachabilityInfo({ status: 'ONLINE' }).label).toBe(ko.shell.reachability.ok);
    expect(reachabilityInfo({ status: 'OFFLINE' })).toEqual({ tone: 'crit', label: ko.shell.reachability.offline });
  });
});

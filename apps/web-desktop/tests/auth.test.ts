import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';

// The token store is module state initialised on import, so each test loads a fresh api module.
const invoke = vi.fn();
vi.mock('@tauri-apps/api/core', () => ({ invoke: (...args: unknown[]) => invoke(...args) }));

const AGENT = 'http://100.64.0.7:9870';
const LEGACY = 'guardian_agent_token';

async function loadApi(tauri: boolean) {
  vi.resetModules();
  if (tauri) (window as any).__TAURI_INTERNALS__ = {};
  const api = await import('../src/api');
  api.setAgentUrl(AGENT);
  return api;
}

function lastAuthHeader(fetchMock: ReturnType<typeof vi.fn>): string | null {
  const init = fetchMock.mock.calls.at(-1)?.[1] as RequestInit;
  return new Headers(init.headers).get('Authorization');
}

let fetchMock: ReturnType<typeof vi.fn>;
let keychain: Map<string, string>;

beforeEach(() => {
  keychain = new Map();
  invoke.mockReset().mockImplementation(async (cmd: string, args: { agent: string; token?: string }) => {
    if (cmd === 'token_get') return keychain.get(args.agent) ?? null;
    if (cmd === 'token_set') keychain.set(args.agent, args.token as string);
    if (cmd === 'token_delete') keychain.delete(args.agent);
    return null;
  });
  fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
  vi.stubGlobal('fetch', fetchMock);
  localStorage.clear();
});

afterEach(() => {
  delete (window as any).__TAURI_INTERNALS__;
  vi.unstubAllGlobals();
});

describe('token storage', () => {
  it('moves a legacy localStorage token into the keychain and deletes it', async () => {
    localStorage.setItem(LEGACY, 'old-token');
    const api = await loadApi(true);
    await api.authFetch(`${AGENT}/api/status`);

    expect(localStorage.getItem(LEGACY)).toBeNull();
    expect(keychain.get(AGENT)).toBe('old-token');
    expect(lastAuthHeader(fetchMock)).toBe('Bearer old-token');
  });

  it('login keeps the token in the keychain and memory, never in localStorage', async () => {
    const api = await loadApi(true);
    expect(await api.login('tok')).toBe(true);
    await api.authFetch(`${AGENT}/api/status`);

    expect(keychain.get(AGENT)).toBe('tok');
    expect(lastAuthHeader(fetchMock)).toBe('Bearer tok');
    expect(Object.values(localStorage)).not.toContain('tok');
  });

  it('a saved token survives a restart', async () => {
    keychain.set(AGENT, 'saved');
    const api = await loadApi(true);
    await api.authFetch(`${AGENT}/api/status`);
    expect(lastAuthHeader(fetchMock)).toBe('Bearer saved');
  });

  it('logout removes the keychain entry and stops sending the token', async () => {
    keychain.set(AGENT, 'saved');
    const api = await loadApi(true);
    await api.logout();
    await api.authFetch(`${AGENT}/api/status`);

    expect(keychain.has(AGENT)).toBe(false);
    expect(lastAuthHeader(fetchMock)).toBeNull();
    const logoutCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/api/auth/logout'));
    expect(logoutCall?.[1]?.method).toBe('POST');
  });

  it('switching agents never sends one agent token to another', async () => {
    keychain.set(AGENT, 'hub-token');
    const api = await loadApi(true);
    api.setAgentUrl('http://other:9870');
    await api.authFetch('http://other:9870/api/status');
    expect(lastAuthHeader(fetchMock)).toBeNull();
  });

  it('outside Tauri the token lives in memory only and the legacy value is dropped', async () => {
    localStorage.setItem(LEGACY, 'old-token');
    const api = await loadApi(false);
    await api.login('tok');
    await api.authFetch(`${AGENT}/api/status`);

    expect(invoke).not.toHaveBeenCalled();
    expect(lastAuthHeader(fetchMock)).toBe('Bearer tok');
    expect(Object.values(localStorage)).not.toContain('tok');
    expect(localStorage.getItem(LEGACY)).toBeNull();
  });

  it('a failing credential store falls back to a memory-only session', async () => {
    invoke.mockRejectedValue(new Error('no keychain'));
    const api = await loadApi(true);
    expect(await api.login('tok')).toBe(true);
    await api.authFetch(`${AGENT}/api/status`);
    expect(lastAuthHeader(fetchMock)).toBe('Bearer tok');
  });
});

describe('agent URL', () => {
  it('the URL chosen in Settings survives a restart, with its saved token', async () => {
    keychain.set(AGENT, 'hub-token');
    await loadApi(true);

    vi.resetModules();
    const restarted = await import('../src/api');
    expect(restarted.getAgentUrl()).toBe(AGENT);
    await restarted.authFetch(`${AGENT}/api/status`);
    expect(lastAuthHeader(fetchMock)).toBe('Bearer hub-token');
  });

  it('the dashboard served by the agent ignores a saved URL', async () => {
    localStorage.setItem('guardian_agent_url', 'http://elsewhere:9870');
    vi.resetModules();
    const api = await import('../src/api');
    expect(api.getAgentUrl()).toBe(window.location.origin);
  });

  it('a plain 404 explains that the agent lacks the endpoint; a JSON 404 keeps its error', async () => {
    const api = await loadApi(true);
    fetchMock.mockResolvedValueOnce(new Response('404 page not found\n', { status: 404 }));
    await expect(api.fetchFleetSSHAudit([], true)).rejects.toThrow(
      ko.shell.api.routeMissing(AGENT, '/api/fleet/ssh')
    );

    fetchMock.mockResolvedValueOnce(new Response('{"error":"persona not found"}', { status: 404 }));
    await expect(api.fetchPersonas()).rejects.toThrow('persona not found');
  });
});

describe('agent health', () => {
  it('reports signed-out on a 401, offline when unreachable', async () => {
    const api = await loadApi(false);
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 401 }));
    expect((await api.checkAgentHealth()).state).toBe('signed-out');
    api.resumeRequests(); // a 401 pauses requests until the user signs in again
    fetchMock.mockRejectedValueOnce(new TypeError('network down'));
    expect((await api.checkAgentHealth()).state).toBe('offline');
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ status: 'healthy' }), { status: 200 }));
    expect((await api.checkAgentHealth()).state).toBe('online');
  });
});

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The signed-out flag is module state, so each test loads a fresh api module and hook.
async function load() {
  vi.resetModules();
  const api = await import('../src/api');
  const { useHubData } = await import('../src/hooks/useHubData');
  api.setAgentUrl('http://hub.test:9870');
  return { api, useHubData };
}

let fetchMock: ReturnType<typeof vi.fn>;
let status = 401;

beforeEach(() => {
  vi.useFakeTimers();
  status = 401;
  fetchMock = vi.fn(async () => new Response('{}', { status }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const tick = (ms: number) => act(async () => vi.advanceTimersByTimeAsync(ms));

describe('polling while signed out', () => {
  it('stops issuing requests after a 401 and resumes after sign-in', async () => {
    const { api, useHubData } = await load();
    const fetcher = () => api.authFetch('http://hub.test:9870/api/status').then((r) => r.json());
    const { result } = renderHook(() => useHubData(fetcher, { pollMs: 1000 }));

    await tick(10);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(api.isSignedOut()).toBe(true);

    await tick(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.error).not.toBeNull();

    status = 200;
    expect(await api.login('tok')).toBe(true);
    expect(api.isSignedOut()).toBe(false);
    const before = fetchMock.mock.calls.length;
    await tick(1000);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
    expect(result.current.data).toEqual({});
  });

  it('resumes when the sign-in dialog is left for Settings', async () => {
    const { api } = await load();
    await expect(api.authFetch('http://hub.test:9870/api/x')).rejects.toBeInstanceOf(api.AuthRequiredError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await expect(api.authFetch('http://hub.test:9870/api/x')).rejects.toBeInstanceOf(api.AuthRequiredError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    api.resumeRequests();
    status = 200;
    await api.authFetch('http://hub.test:9870/api/x');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('resumes when another hub is selected', async () => {
    const { api } = await load();
    await expect(api.authFetch('http://hub.test:9870/api/x')).rejects.toBeInstanceOf(api.AuthRequiredError);
    api.setAgentUrl('http://other.test:9870');
    status = 200;
    await api.authFetch('http://other.test:9870/api/x');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

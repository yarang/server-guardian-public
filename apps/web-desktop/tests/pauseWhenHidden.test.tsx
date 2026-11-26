import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Module state (signed-out flag) is per copy, so each test loads a fresh api module and hook.
async function load() {
  vi.resetModules();
  const api = await import('../src/api');
  const { useHubData } = await import('../src/hooks/useHubData');
  api.setAgentUrl('http://hub.test:9870');
  return { api, useHubData };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn(async () => new Response('{}', { status: 200 }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  // Drop the own property the tests put on the document; the prototype getter takes over again.
  Reflect.deleteProperty(document, 'visibilityState');
});

const tick = (ms: number) => act(async () => vi.advanceTimersByTimeAsync(ms));

/** Sets `document.visibilityState` and fires the event a browser would fire with it. */
function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('useHubData pauseWhenHidden', () => {
  it('skips interval fetches while hidden and fetches once right after becoming visible', async () => {
    const { api, useHubData } = await load();
    const fetcher = () => api.authFetch('http://hub.test:9870/api/x').then((r) => r.json());
    renderHook(() => useHubData(fetcher, { pollMs: 1000, pauseWhenHidden: true }));

    await tick(10);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await tick(1000);
    await tick(1000);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    // A hidden tab stops the poll: five interval ticks, no request.
    setVisibility('hidden');
    await tick(5000);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    // Coming back fetches immediately, then the interval resumes.
    setVisibility('visible');
    await tick(0);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await tick(1000);
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it('still polls while hidden without the option (existing callers unchanged)', async () => {
    const { api, useHubData } = await load();
    const fetcher = () => api.authFetch('http://hub.test:9870/api/x').then((r) => r.json());
    renderHook(() => useHubData(fetcher, { pollMs: 1000 }));

    setVisibility('hidden');
    await tick(1000);
    await tick(1000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    // No visibilitychange listener without the option: the event itself fetches nothing.
    setVisibility('visible');
    await tick(0);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('keeps a manual reload working while hidden and cleans the listener up on unmount', async () => {
    const { api, useHubData } = await load();
    const fetcher = () => api.authFetch('http://hub.test:9870/api/x').then((r) => r.json());
    const { result, unmount } = renderHook(() => useHubData(fetcher, { pollMs: 1000, pauseWhenHidden: true }));
    await tick(10);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // The refresh button works in a hidden tab too: a person asked for this data.
    setVisibility('hidden');
    act(() => result.current.reload());
    await tick(10);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // After unmount neither the interval nor the visibility listener fetches.
    unmount();
    setVisibility('visible');
    await tick(5000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

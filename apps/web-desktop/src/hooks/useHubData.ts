import { useCallback, useEffect, useRef, useState } from 'react';
import { AuthRequiredError } from '../api';
import { errorText } from '../lib/format';
import { useRefreshToken } from './refresh';

export interface HubDataState<T> {
  /** Last successful result; cleared to null whenever a fetch fails so stale numbers never look live. */
  data: T | null;
  /** The failure message of the latest fetch, or null. */
  error: string | null;
  /** A fetch is in flight (initial, retry, refresh or poll). */
  loading: boolean;
  /** Fetch again now. */
  reload: () => void;
}

export interface HubDataOptions {
  /** Poll interval in ms; omitted means fetch once per refresh token / reload. */
  pollMs?: number;
  /**
   * Pause the poll while the document is hidden (a background tab): the interval skips its fetch,
   * and one `visibilitychange` back to visible fetches immediately and resumes polling. Meant for
   * polls that cost the hub a process spawn (the resident monitor); defaults to false so existing
   * callers keep polling in background tabs.
   */
  pauseWhenHidden?: boolean;
}

/**
 * Fetch hub data with optional polling. Refetches when the global Refresh token changes, aborts
 * (ignores) in-flight results on unmount, and surfaces failures as `error` instead of fake data.
 */
export function useHubData<T>(fetcher: (signal: AbortSignal) => Promise<T>, options: HubDataOptions = {}): HubDataState<T> {
  const { pollMs, pauseWhenHidden } = options;
  const refreshToken = useRefreshToken();
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [manual, setManual] = useState(0);

  useEffect(() => {
    const controller = new AbortController();

    const load = async () => {
      setLoading(true);
      try {
        const result = await fetcherRef.current(controller.signal);
        if (controller.signal.aborted) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof AuthRequiredError) {
          window.dispatchEvent(new CustomEvent('guardian:auth-required'));
        }
        setData(null);
        setError(errorText(err));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };

    // A hidden tab skips every interval tick; coming back fetches once right away.
    const hidden = () => pauseWhenHidden && document.visibilityState === 'hidden';
    const onVisibility = () => {
      if (!hidden()) void load();
    };

    void load();
    const timer = pollMs
      ? window.setInterval(() => {
          if (!hidden()) void load();
        }, pollMs)
      : undefined;
    if (pollMs && pauseWhenHidden) document.addEventListener('visibilitychange', onVisibility);
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearInterval(timer);
      if (pollMs && pauseWhenHidden) document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [refreshToken, manual, pollMs, pauseWhenHidden]);

  const reload = useCallback(() => setManual((n) => n + 1), []);
  return { data, error, loading, reload };
}

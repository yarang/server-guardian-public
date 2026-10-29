import { useCallback, useEffect, useRef, useState } from 'react';
import { AuthRequiredError, cancelDebate, fetchDebate } from '../api';
import { errorText } from '../lib/format';
import type { DebateJob, DebateJobDetail } from '../types';
import { tr } from '../i18n';

export const POLL_MS = 3000;

export interface DebateJobState {
  job: DebateJobDetail | null;
  /** Latest status-refresh or cancel failure, or null. */
  error: string | null;
  cancelling: boolean;
  /** Show a job that was just started (no log or result yet); polling begins if it is running. */
  begin: (job: DebateJob) => void;
  /** Load and show an existing job; rejects with the failure message. */
  open: (id: string) => Promise<void>;
  cancel: () => Promise<void>;
}

/**
 * The debate being watched. While it is running the hook fetches it every 3s with one interval
 * (keyed by job id and running state, so it never stacks), stops when it finishes, switches to
 * another job, or the component unmounts, and ignores responses for a job no longer shown.
 * `onFinished` runs once when a poll sees the job leave the running state.
 */
export function useDebateJob(onFinished?: () => void): DebateJobState {
  const [job, setJob] = useState<DebateJobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const currentId = useRef<string | null>(null);
  const statusRef = useRef<string | null>(null);
  const alive = useRef(true);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /** Fetch the shown job once; returns true when the job left the running state. */
  const refresh = useCallback(async (id: string): Promise<boolean> => {
    try {
      const next = await fetchDebate(id);
      if (!alive.current || currentId.current !== id) return false;
      const finished = statusRef.current === 'running' && next.status !== 'running';
      statusRef.current = next.status;
      setJob(next);
      setError(null);
      return finished;
    } catch (err) {
      if (err instanceof AuthRequiredError) return false;
      if (alive.current && currentId.current === id) setError(tr.debate.hooks.statusRefreshFailed(errorText(err)));
      return false;
    }
  }, []);

  const running = job?.status === 'running';
  const jobId = job?.id ?? null;
  useEffect(() => {
    if (!running || jobId === null) return;
    let inFlight = false;
    const timer = window.setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void refresh(jobId)
        .then((finished) => {
          if (finished) finishedRef.current?.();
        })
        .finally(() => {
          inFlight = false;
        });
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, jobId, refresh]);

  const begin = useCallback((started: DebateJob) => {
    currentId.current = started.id;
    statusRef.current = started.status;
    setJob({ ...started, log: '', result: null });
    setError(null);
    setCancelling(false);
  }, []);

  const open = useCallback(async (id: string) => {
    const detail = await fetchDebate(id);
    if (!alive.current) return;
    currentId.current = detail.id;
    statusRef.current = detail.status;
    setJob(detail);
    setError(null);
    setCancelling(false);
  }, []);

  const cancel = useCallback(async () => {
    const id = currentId.current;
    if (id === null) return;
    setCancelling(true);
    try {
      await cancelDebate(id);
      await refresh(id);
    } catch (err) {
      if (!(err instanceof AuthRequiredError) && alive.current) setError(tr.debate.hooks.cancelFailed(errorText(err)));
    } finally {
      if (alive.current) setCancelling(false);
      finishedRef.current?.();
    }
  }, [refresh]);

  return { job, error, cancelling, begin, open, cancel };
}

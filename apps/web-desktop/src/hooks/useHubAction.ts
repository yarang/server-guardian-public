import { useCallback, useEffect, useRef, useState } from 'react';
import { AuthRequiredError } from '../api';
import { errorText } from '../lib/format';

export interface HubActionState<A extends unknown[], T> {
  run: (...args: A) => Promise<void>;
  data: T | null;
  error: string | null;
  running: boolean;
  reset: () => void;
}

/** On-demand hub call (button-triggered). Failure clears data and sets `error`; unmount ignores late results. */
export function useHubAction<A extends unknown[], T>(fn: (...args: A) => Promise<T>): HubActionState<A, T> {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const alive = useRef(true);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const run = useCallback(async (...args: A) => {
    setRunning(true);
    setError(null);
    try {
      const result = await fnRef.current(...args);
      if (alive.current) setData(result);
    } catch (err) {
      if (err instanceof AuthRequiredError) window.dispatchEvent(new CustomEvent('guardian:auth-required'));
      if (alive.current) {
        setData(null);
        setError(errorText(err));
      }
    } finally {
      if (alive.current) setRunning(false);
    }
  }, []);

  const reset = useCallback(() => {
    setData(null);
    setError(null);
  }, []);

  return { run, data, error, running, reset };
}

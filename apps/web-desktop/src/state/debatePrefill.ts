import { useSyncExternalStore } from 'react';
import type { OpsScope } from '../lib/project';

/**
 * Hand-off from other pages (e.g. the Overview page "Debate this") to the New debate form on the Debates page.
 *
 * Producer:  setDebatePrefill(task, source); navigate('debate');
 * Consumer:  const prefill = useDebatePrefill();
 *            useEffect(() => { if (prefill) { setTask(prefill.task); clearDebatePrefill(); } }, [prefill]);
 */
export interface DebatePrefill {
  /** Text to place in the Debate task input. */
  task: string;
  /** Who produced it, e.g. 'overview' or 'node'. */
  source: string;
  /** Set by fleet, node and group hand-offs: the form switches to the ops project of that scope (lib/project opsProjectFor). */
  ops?: OpsScope;
  /** Date.now() when set; changes on every set so identical text still notifies. */
  createdAt: number;
}

let current: DebatePrefill | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  listeners.forEach((l) => l());
}

export function setDebatePrefill(task: string, source = 'unknown', ops?: OpsScope): void {
  current = { task, source, ops, createdAt: Date.now() };
  emit();
}

export function getDebatePrefill(): DebatePrefill | null {
  return current;
}

export function clearDebatePrefill(): void {
  if (current === null) return;
  current = null;
  emit();
}

/** Returns the pending prefill and clears it (for non-React callers). */
export function consumeDebatePrefill(): DebatePrefill | null {
  const taken = current;
  clearDebatePrefill();
  return taken;
}

export function subscribeDebatePrefill(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Pending prefill, or null. Re-renders when it is set or cleared. */
export function useDebatePrefill(): DebatePrefill | null {
  return useSyncExternalStore(subscribeDebatePrefill, getDebatePrefill, getDebatePrefill);
}

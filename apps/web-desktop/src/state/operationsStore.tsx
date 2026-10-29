import { tr } from '../i18n';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { AuthRequiredError, HubError, publishThread, runAction } from '../api';
import { failMessage } from '../lib/debate';
import type { ActionItem, PublishThreadResponse } from '../types';

/** Outcome of an Execute (action run) that the operator may have left the page during. */
export type ExecEntry =
  | { phase: 'running' }
  | { phase: 'done'; action: ActionItem }
  | { phase: 'error'; message: string; status?: number };

/** Outcome of a Publish. `done` carries the hub's answer (which may itself be a failed push). */
export type PublishEntry =
  | { phase: 'running' }
  | { phase: 'done'; response: PublishThreadResponse }
  | { phase: 'error'; message: string };

export interface OperationsStore {
  /** Action runs by action id (kept until {@link OperationsStore.ackExec}). */
  execs: Record<number, ExecEntry>;
  /** Publishes by thread id (kept until {@link OperationsStore.clearPublish}). */
  publishes: Record<string, PublishEntry>;
  /** An Execute is in flight (drives the Approvals nav indicator). */
  execRunning: boolean;
  /** A Publish is in flight (drives the Debate nav indicator). */
  publishRunning: boolean;
  startExec: (action: ActionItem) => void;
  /** The dialog showed the outcome; forget it. */
  ackExec: (id: number) => void;
  startPublish: (thread: string, branch: string) => void;
  clearPublish: (thread: string) => void;
}

const OperationsContext = createContext<OperationsStore | null>(null);

/**
 * App-level owner of the long synchronous hub calls (Execute up to ~15 minutes, Publish with the project's tests).
 * They live above the pages, so leaving the page does not lose the outcome: coming back shows the final result.
 * `onSettled` runs when any of them finishes (the app uses it to refresh the approvals list).
 */
export function OperationsStoreProvider({ children, onSettled }: { children: ReactNode; onSettled?: () => void }) {
  const [execs, setExecs] = useState<Record<number, ExecEntry>>({});
  const [publishes, setPublishes] = useState<Record<string, PublishEntry>>({});
  const alive = useRef(true);
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;
  // Synchronous guards: state updates lag a click behind, a second click must not start a second run.
  const execBusy = useRef(new Set<number>());
  const publishBusy = useRef(new Set<string>());

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const startExec = useCallback((a: ActionItem) => {
    if (execBusy.current.has(a.id)) return;
    execBusy.current.add(a.id);
    setExecs((m) => ({ ...m, [a.id]: { phase: 'running' } }));
    void (async () => {
      let entry: ExecEntry;
      try {
        const r = await runAction(a.id);
        let next = r.action || a;
        if ((next.result === null || next.result === undefined) && r.result) next = { ...next, result: r.result };
        entry = { phase: 'done', action: next };
      } catch (err) {
        if (err instanceof AuthRequiredError) {
          window.dispatchEvent(new CustomEvent('guardian:auth-required'));
          entry = { phase: 'error', message: tr.ops.approvals.signInAgain };
        } else {
          entry = { phase: 'error', message: failMessage(err, 'execute', a.id), status: err instanceof HubError ? err.status : undefined };
        }
      }
      execBusy.current.delete(a.id);
      if (!alive.current) return;
      setExecs((m) => ({ ...m, [a.id]: entry }));
      settledRef.current?.();
    })();
  }, []);

  const ackExec = useCallback((id: number) => {
    setExecs((m) => {
      if (!m[id] || m[id].phase === 'running') return m;
      const { [id]: _gone, ...rest } = m;
      return rest;
    });
  }, []);

  const startPublish = useCallback((thread: string, branch: string) => {
    if (publishBusy.current.has(thread)) return;
    publishBusy.current.add(thread);
    setPublishes((m) => ({ ...m, [thread]: { phase: 'running' } }));
    void (async () => {
      let entry: PublishEntry | null;
      try {
        entry = { phase: 'done', response: await publishThread(thread, branch.trim() || undefined) };
      } catch (err) {
        if (err instanceof AuthRequiredError) {
          window.dispatchEvent(new CustomEvent('guardian:auth-required'));
          entry = null;
        } else {
          entry = { phase: 'error', message: failMessage(err, 'publish', thread) };
        }
      }
      publishBusy.current.delete(thread);
      if (!alive.current) return;
      setPublishes((m) => {
        if (entry) return { ...m, [thread]: entry };
        const { [thread]: _gone, ...rest } = m;
        return rest;
      });
      settledRef.current?.();
    })();
  }, []);

  const clearPublish = useCallback((thread: string) => {
    setPublishes((m) => {
      if (!m[thread] || m[thread].phase === 'running') return m;
      const { [thread]: _gone, ...rest } = m;
      return rest;
    });
  }, []);

  const store = useMemo<OperationsStore>(
    () => ({
      execs,
      publishes,
      execRunning: Object.values(execs).some((e) => e.phase === 'running'),
      publishRunning: Object.values(publishes).some((e) => e.phase === 'running'),
      startExec,
      ackExec,
      startPublish,
      clearPublish,
    }),
    [execs, publishes, startExec, ackExec, startPublish, clearPublish]
  );
  return <OperationsContext.Provider value={store}>{children}</OperationsContext.Provider>;
}

/** Long-running hub operations shared across pages; must be used under {@link OperationsStoreProvider}. */
export function useOperationsStore(): OperationsStore {
  const store = useContext(OperationsContext);
  if (store === null) throw new Error('useOperationsStore must be used inside OperationsStoreProvider');
  return store;
}

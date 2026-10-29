import { useCallback, useEffect, useState } from 'react';
import { AuthRequiredError, EVENTS_PAGE_SIZE, pollDebateEvents, streamDebateEvents } from '../api';
import { errorText } from '../lib/format';
import { finalizeRun, initialRunState, reduceEvents } from '../lib/agentMonitor';
import type { RunState } from '../lib/agentMonitor';
import type { DebateEvent } from '../types';

export type EventsPhase = 'loading' | 'live' | 'reconnecting' | 'finished' | 'error';

export interface DebateEventsState {
  run: RunState;
  phase: EventsPhase;
  error: string | null;
  /** Status of the hub's final `end` line, when one arrived. */
  endStatus: string | null;
  reload: () => void;
}

const FLUSH_MS = 100;

/**
 * Event history and live follow of one debate job. It polls from seq 0 (paging past 1000 events);
 * a finished job stops there, a running one continues on the NDJSON stream, which reconnects on
 * its own, and a last poll after `end` picks up anything written between the two. Pass null to
 * stay idle. Stream bursts are folded in batches so a long replay renders once, not per event.
 */
export function useDebateEvents(id: string | null): DebateEventsState {
  const [run, setRun] = useState<RunState>(initialRunState);
  const [phase, setPhase] = useState<EventsPhase>('loading');
  const [error, setError] = useState<string | null>(null);
  const [endStatus, setEndStatus] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let state = initialRunState();
    setRun(state);
    setError(null);
    setEndStatus(null);
    if (id === null) {
      setPhase('finished');
      return;
    }
    setPhase('loading');

    const controller = new AbortController();
    const { signal } = controller;
    let pending: DebateEvent[] = [];
    let timer: number | undefined;

    const flush = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
      if (pending.length === 0 || signal.aborted) return;
      state = reduceEvents(state, pending);
      pending = [];
      setRun(state);
    };
    const push = (events: DebateEvent[]) => {
      pending.push(...events);
      if (timer === undefined) timer = window.setTimeout(flush, FLUSH_MS);
    };

    /** Polls pages after `after` until one is short; returns the cursor and the final `done`. */
    const pollAll = async (after: number): Promise<{ next: number; done: boolean }> => {
      let cursor = after;
      let done = false;
      for (let page = 0; page < 500; page++) {
        const r = await pollDebateEvents(id, cursor);
        if (signal.aborted) return { next: cursor, done };
        push(r.events);
        cursor = Math.max(cursor, r.next);
        done = r.done;
        if (r.events.length < EVENTS_PAGE_SIZE) break;
      }
      return { next: cursor, done };
    };

    (async () => {
      try {
        const first = await pollAll(0);
        if (signal.aborted) return;
        flush();
        if (first.done) {
          setPhase('finished');
          return;
        }
        setPhase('live');
        const result = await streamDebateEvents(
          id,
          first.next,
          (msg) => {
            if (msg.type === 'heartbeat') return;
            if (msg.type === 'end') return;
            push([msg]);
          },
          signal,
          { onConnection: (c) => !signal.aborted && setPhase(c) }
        );
        if (signal.aborted) return;
        flush();
        if (result.status) setEndStatus(result.status);
        await pollAll(result.lastSeq);
        if (signal.aborted) return;
        flush();
        setPhase('finished');
      } catch (err) {
        if (signal.aborted) return;
        flush();
        if (!(err instanceof AuthRequiredError)) setError(errorText(err));
        setPhase('error');
      }
    })();

    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [id, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { run, phase, error, endStatus, reload };
}

/** The run state with leftover in-flight lanes settled once the job is known to be over. */
export function settledRun(run: RunState, jobStatus: string | undefined, phase: EventsPhase): RunState {
  if (!jobStatus || jobStatus === 'running' || phase === 'loading') return run;
  return finalizeRun(run, jobStatus);
}

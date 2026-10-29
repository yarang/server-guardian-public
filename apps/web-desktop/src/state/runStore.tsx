import { tr } from '../i18n';
import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { TerminalLine } from '@server-guardian/ui';
import { executeCommand, executeRemoteSSH, streamFleetExec } from '../api';
import { fixed } from '../lib/format';
import { resultLines } from '../lib/terminalLines';
import type { FleetStreamEvent } from '../types';
import { useHubStore } from './hubStore';

/** Target value for the agent node itself; the default because it needs no confirmation. */
export const LOCAL_TARGET = 'local';
/** Target value for the whole fleet. */
export const ALL_TARGET = 'all';
/** Target values for node groups; single nodes use their plain id. */
export const GROUP_PREFIX = 'group:';

export type RowStatus = 'WAITING' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'NO RESULT';

export interface RunRow {
  id: string;
  name: string;
  status: RowStatus;
  exitCode?: number;
  durationMs?: number;
  output?: string;
}

/** State of the latest group / fleet run (the one with a progress meter and a per-node table). */
export interface MultiRunState {
  phase: 'idle' | 'running' | 'done' | 'error' | 'cancelled';
  rows: Record<string, RunRow>;
  order: string[];
  lines: TerminalLine[];
  total: number;
  completed: number;
  error: string | null;
}

type Action =
  | { type: 'begin'; label: string; command: string; servers: Array<{ id: string; name: string }>; total: number }
  | { type: 'event'; event: FleetStreamEvent }
  | { type: 'fail'; message: string }
  | { type: 'cancel' };

const initial: MultiRunState = { phase: 'idle', rows: {}, order: [], lines: [], total: 0, completed: 0, error: null };

function firstLine(text: string | undefined): string {
  return (text ?? '').split('\n').find((l) => l.trim() !== '')?.trim() ?? '';
}

function upsert(state: MultiRunState, row: RunRow): MultiRunState {
  return {
    ...state,
    rows: { ...state.rows, [row.id]: row },
    order: state.order.includes(row.id) ? state.order : [...state.order, row.id],
  };
}

/** Rows that never reported are marked, not left looking like they are still in progress. */
function settleUnfinished(rows: Record<string, RunRow>): Record<string, RunRow> {
  const out: Record<string, RunRow> = {};
  for (const [id, r] of Object.entries(rows)) {
    out[id] = r.status === 'WAITING' || r.status === 'RUNNING' ? { ...r, status: 'NO RESULT' } : r;
  }
  return out;
}

/** Exported for unit tests; the provider is the only production caller. */
export function multiReducer(state: MultiRunState, action: Action): MultiRunState {
  switch (action.type) {
    case 'begin': {
      const rows: Record<string, RunRow> = {};
      for (const s of action.servers) rows[s.id] = { id: s.id, name: s.name, status: 'WAITING' };
      return {
        phase: 'running',
        rows,
        order: action.servers.map((s) => s.id),
        lines: [{ kind: 'prompt', text: `[${action.label}] ${action.command}` }],
        total: action.total,
        completed: 0,
        error: null,
      };
    }
    case 'event': {
      const ev = action.event;
      switch (ev.type) {
        case 'start':
          return {
            ...state,
            total: ev.total ?? state.total,
            lines: [...state.lines, { kind: 'meta', text: tr.ops.run.store.started(ev.total ?? state.total) }],
          };
        case 'node_start':
          return ev.node ? upsert(state, { id: ev.node.id, name: ev.node.name, status: 'RUNNING' }) : state;
        case 'node_done': {
          const r = ev.result;
          if (!r) return state;
          const next = upsert(state, {
            id: r.id,
            name: r.name,
            status: r.exit_code === 0 ? 'SUCCESS' : 'FAILED',
            exitCode: r.exit_code,
            durationMs: r.duration_ms,
            output: firstLine(r.stdout) || firstLine(r.stderr),
          });
          return {
            ...next,
            completed: ev.completed ?? state.completed + 1,
            lines: [...next.lines, ...resultLines(r, r.name)],
          };
        }
        case 'complete':
          return {
            ...state,
            phase: 'done',
            lines: [
              ...state.lines,
              {
                kind: 'meta',
                text: tr.ops.run.store.complete(ev.success ?? 0, ev.failed ?? 0, fixed(ev.elapsed_sec, 2)),
              },
            ],
          };
        case 'error': {
          const message = ev.message || tr.ops.run.store.hubError;
          return {
            ...state,
            phase: 'error',
            error: message,
            rows: settleUnfinished(state.rows),
            lines: [...state.lines, { kind: 'err', text: tr.ops.run.store.fleetFailed(message) }],
          };
        }
        default:
          return state;
      }
    }
    case 'fail':
      return {
        ...state,
        phase: 'error',
        error: action.message,
        rows: settleUnfinished(state.rows),
        lines: [...state.lines, { kind: 'err', text: tr.ops.run.store.fleetFailed(action.message) }],
      };
    case 'cancel':
      return {
        ...state,
        phase: 'cancelled',
        rows: settleUnfinished(state.rows),
        lines: [
          ...state.lines,
          { kind: 'meta', text: tr.ops.run.store.stopped },
        ],
      };
  }
}

/** What the Run page needs from the store. */
export interface RunStore {
  /** Selected target: `local`, `all`, `group:<id>` or a node id. */
  target: string;
  /** Change the target by hand. Ignored while a group / fleet run is in progress. */
  setTarget: (target: string) => void;
  /** Apply a `?target=` from the URL: ignored when empty or while a group / fleet run is in progress. */
  offerTarget: (target: string | undefined) => void;
  /** Terminal history shared by all single-target runs (local node or one node). */
  history: TerminalLine[];
  /** A single-target command is in flight. */
  singleRunning: boolean;
  /** The latest group / fleet run. */
  multi: MultiRunState;
  /** A group / fleet run is streaming (drives the nav indicator). */
  multiRunning: boolean;
  /** Run on the local node or one node; the output is appended to {@link history}. */
  runSingle: (target: string, label: string, command: string) => void;
  /** Stream a command over a group (`ids`) or the fleet (`ids` empty); replaces any earlier stream. */
  runMulti: (args: { label: string; command: string; ids: string[]; rows: Array<{ id: string; name: string }>; timeoutSec?: number }) => void;
  /** Cancel the active stream ("Stop watching"); nodes that already started keep running. */
  stopWatching: () => void;
}

const RunStoreContext = createContext<RunStore | null>(null);

/**
 * App-level owner of Run state: the selected target, the terminal history and the group / fleet
 * stream. It lives above the pages, so leaving Run does not cancel a stream and coming back shows
 * the live progress. The stream is cancelled only by {@link RunStore.stopWatching}, by a new run
 * replacing it, or when the whole app unmounts. Must be mounted inside HubStoreProvider.
 */
export function RunStoreProvider({ children }: { children: ReactNode }) {
  const { servers, groups } = useHubStore();
  const [target, setTargetState] = useState(LOCAL_TARGET);
  const [multi, dispatch] = useReducer(multiReducer, initial);
  const [history, setHistory] = useState<TerminalLine[]>([]);
  const [singleRunning, setSingleRunning] = useState(false);
  const cancelRef = useRef<(() => void) | null>(null);
  /** Bumped per stream; callbacks of a replaced or stopped stream compare against it and drop their events. */
  const streamId = useRef(0);
  const alive = useRef(true);
  const singleBusy = useRef(false);

  const multiRunning = multi.phase === 'running';
  const multiRunningRef = useRef(false);
  multiRunningRef.current = multiRunning;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      streamId.current += 1;
      cancelRef.current?.();
      cancelRef.current = null;
    };
  }, []);

  const setTarget = useCallback((next: string) => {
    if (!multiRunningRef.current) setTargetState(next);
  }, []);
  const offerTarget = useCallback((next: string | undefined) => {
    if (next && !multiRunningRef.current) setTargetState(next);
  }, []);

  // A target the hub no longer lists falls back to the local node (never mid-run).
  useEffect(() => {
    if (multiRunning || target === LOCAL_TARGET || target === ALL_TARGET) return;
    if (target.startsWith(GROUP_PREFIX)) {
      if (groups.data && !groups.data.groups.some((g) => GROUP_PREFIX + g.id === target)) setTargetState(LOCAL_TARGET);
    } else if (servers.data && !servers.data.some((s) => s.id === target)) {
      setTargetState(LOCAL_TARGET);
    }
  }, [servers.data, groups.data, target, multiRunning]);

  const runSingle = useCallback((on: string, label: string, command: string) => {
    if (singleBusy.current) {
      setHistory((l) => [...l, { kind: 'meta', text: tr.ops.run.store.stillRunning }]);
      return;
    }
    singleBusy.current = true;
    setSingleRunning(true);
    setHistory((l) => [...l, { kind: 'prompt', text: `[${label}] ${command}` }]);
    void (async () => {
      const res = on === LOCAL_TARGET ? await executeCommand(command) : await executeRemoteSSH(on, command);
      singleBusy.current = false;
      if (!alive.current) return;
      setHistory((l) => [...l, ...resultLines(res)]);
      setSingleRunning(false);
    })();
  }, []);

  const runMulti = useCallback<RunStore['runMulti']>(({ label, command, ids, rows, timeoutSec }) => {
    cancelRef.current?.();
    const id = (streamId.current += 1);
    dispatch({ type: 'begin', label, command, servers: rows, total: rows.length });
    cancelRef.current = streamFleetExec(
      command,
      ids,
      (event) => {
        if (!alive.current || streamId.current !== id) return;
        if (event.type === 'complete' || event.type === 'error') cancelRef.current = null;
        dispatch({ type: 'event', event });
      },
      () => {
        if (!alive.current || streamId.current !== id) return;
        cancelRef.current = null;
        dispatch({
          type: 'fail',
          message: tr.ops.run.store.streamLost,
        });
      },
      timeoutSec
    );
  }, []);

  const stopWatching = useCallback(() => {
    streamId.current += 1;
    cancelRef.current?.();
    cancelRef.current = null;
    dispatch({ type: 'cancel' });
  }, []);

  const store = useMemo<RunStore>(
    () => ({ target, setTarget, offerTarget, history, singleRunning, multi, multiRunning, runSingle, runMulti, stopWatching }),
    [target, setTarget, offerTarget, history, singleRunning, multi, multiRunning, runSingle, runMulti, stopWatching]
  );
  return <RunStoreContext.Provider value={store}>{children}</RunStoreContext.Provider>;
}

/** Run state shared across pages; must be used under {@link RunStoreProvider}. */
export function useRunStore(): RunStore {
  const store = useContext(RunStoreContext);
  if (store === null) throw new Error('useRunStore must be used inside RunStoreProvider');
  return store;
}

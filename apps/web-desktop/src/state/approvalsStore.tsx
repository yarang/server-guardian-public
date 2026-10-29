import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { fetchClosedThreads, listActions } from '../api';
import { useHubData } from '../hooks/useHubData';
import { isAwaitingDecision, isHeld } from '../lib/debate';
import type { ActionItem } from '../types';
import { useProjectStore } from './projectStore';

/** Poll interval of the approvals count (the badge stays roughly current while other pages are open). */
export const APPROVALS_POLL_MS = 30_000;

export interface ApprovalsStore {
  /** All actions of the selected project (null until loaded, and after a failed fetch). */
  actions: ActionItem[] | null;
  error: string | null;
  loading: boolean;
  /** Ids of the CLOSED threads (empty when the hub does not list them). Closing a thread does not change its actions. */
  closedThreads: ReadonlySet<string>;
  /** PROPOSED count, or null while unknown (not loaded or failed); never a guess. */
  waiting: number | null;
  /** HELD count, or null while unknown. */
  held: number | null;
  /** Bumped by every {@link ApprovalsStore.refresh}; lists use it to refetch their own filtered view. */
  version: number;
  /** Fetch again now and bump `version`; call after an action was run, rejected or re-checked. */
  refresh: () => void;
}

const ApprovalsStoreContext = createContext<ApprovalsStore | null>(null);

interface Loaded {
  project: string;
  list: ActionItem[];
  closed: string[];
}

/**
 * Actions of the selected project, owned above the pages so the nav badge and the Overview card
 * read one fetch. Refetches on a 30 s poll, the global Refresh token, a project switch and `refresh()`.
 */
export function ApprovalsStoreProvider({ children }: { children: ReactNode }) {
  const { selected } = useProjectStore();
  const state = useHubData<Loaded>(
    async () => {
      // The closed-thread list is decoration: an older hub without it must not hide the actions.
      const [list, closed] = await Promise.all([listActions(undefined, selected), fetchClosedThreads().catch(() => [] as string[])]);
      return { project: selected, list, closed };
    },
    { pollMs: APPROVALS_POLL_MS }
  );
  const [version, setVersion] = useState(0);

  // Switching project refetches (the poll alone would show the old project's count for up to 30 s).
  const reload = state.reload;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    reload();
  }, [selected, reload]);

  const refresh = useCallback(() => {
    setVersion((n) => n + 1);
    reload();
  }, [reload]);

  const store = useMemo<ApprovalsStore>(() => {
    // Data fetched for another project is not shown while the new project's answer is in flight.
    const actions = state.data && state.data.project === selected ? state.data.list : null;
    return {
      actions,
      error: state.error,
      closedThreads: new Set(state.data && state.data.project === selected ? state.data.closed : []),
      loading: state.loading,
      waiting: actions ? actions.filter(isAwaitingDecision).length : null,
      held: actions ? actions.filter(isHeld).length : null,
      version,
      refresh,
    };
  }, [state.data, state.error, state.loading, selected, version, refresh]);

  return <ApprovalsStoreContext.Provider value={store}>{children}</ApprovalsStoreContext.Provider>;
}

/** Like {@link useApprovalsStore} but null outside the provider (for dialogs that only decorate with it). */
export function useOptionalApprovalsStore(): ApprovalsStore | null {
  return useContext(ApprovalsStoreContext);
}

/** Shared actions and counts; must be used under {@link ApprovalsStoreProvider}. */
export function useApprovalsStore(): ApprovalsStore {
  const store = useContext(ApprovalsStoreContext);
  if (store === null) throw new Error('useApprovalsStore must be used inside ApprovalsStoreProvider');
  return store;
}

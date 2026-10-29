import { createContext, useContext, useEffect, useMemo, useRef } from 'react';
import type { ReactNode } from 'react';
import { fetchDebates } from '../api';
import { useDebateJob } from '../hooks/useDebateJob';
import type { DebateJobState } from '../hooks/useDebateJob';
import { useHubData } from '../hooks/useHubData';
import type { HubDataState } from '../hooks/useHubData';
import type { DebateJob } from '../types';
import { useProjectStore } from './projectStore';

export interface DebateStore {
  /** The watched debate and its polling; lives above the pages, so leaving Debate does not stop it. */
  watcher: DebateJobState;
  /** Recent debates of the selected project. */
  recent: HubDataState<DebateJob[]>;
}

const DebateStoreContext = createContext<DebateStore | null>(null);

/**
 * Owns the watched debate job (polling every 3 s while it runs, stopping when it finishes or this
 * provider unmounts) and the recent-debates list, so returning to Debate shows the same live job.
 */
export function DebateStoreProvider({ children }: { children: ReactNode }) {
  const { selected } = useProjectStore();
  const recent = useHubData<DebateJob[]>(() => fetchDebates(selected));
  const reloadRecent = recent.reload;
  const watcher = useDebateJob(reloadRecent);

  // A project switch lists that project's debates (the first run is the initial fetch).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    reloadRecent();
  }, [selected, reloadRecent]);

  const store = useMemo<DebateStore>(() => ({ watcher, recent }), [watcher, recent]);
  return <DebateStoreContext.Provider value={store}>{children}</DebateStoreContext.Provider>;
}

/** Shared watched job and recent debates; must be used under {@link DebateStoreProvider}. */
export function useDebateStore(): DebateStore {
  const store = useContext(DebateStoreContext);
  if (store === null) throw new Error('useDebateStore must be used inside DebateStoreProvider');
  return store;
}

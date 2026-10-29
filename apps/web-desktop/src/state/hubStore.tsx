import { createContext, useCallback, useContext, useMemo } from 'react';
import type { ReactNode } from 'react';
import { diagnoseFleet, fetchGroups, fetchServers } from '../api';
import { useHubData } from '../hooks/useHubData';
import type { HubDataState } from '../hooks/useHubData';
import type { FleetDiagnosisReport, NodeGroupList, FleetNode } from '../types';

/** The shared fleet diagnosis: HubDataState-compatible, plus when it was measured and a re-probe. */
export interface DiagnosisState extends HubDataState<FleetDiagnosisReport> {
  /** Epoch ms when the current `data` finished probing, or null until a probe succeeds. */
  measuredAt: number | null;
  /** Probe the whole fleet again (same as `reload`). */
  rerun: () => void;
}

export interface HubStore {
  servers: HubDataState<FleetNode[]>;
  groups: HubDataState<NodeGroupList>;
  diagnosis: DiagnosisState;
}

interface Measured {
  report: FleetDiagnosisReport;
  measuredAt: number;
}

const HubStoreContext = createContext<HubStore | null>(null);

/**
 * App-level store for data several pages need: servers, node groups and the fleet diagnosis.
 * Each is fetched once per app session and again only on the global Refresh token (or `reload` /
 * `rerun`), so moving between pages never re-probes the fleet.
 */
export function HubStoreProvider({ children }: { children: ReactNode }) {
  const servers = useHubData(fetchServers);
  const groups = useHubData(fetchGroups);
  const probe = useHubData<Measured>(async () => ({ report: await diagnoseFleet(), measuredAt: Date.now() }));
  const reloadProbe = probe.reload;
  const rerun = useCallback(() => reloadProbe(), [reloadProbe]);

  const diagnosis = useMemo<DiagnosisState>(
    () => ({
      data: probe.data?.report ?? null,
      error: probe.error,
      loading: probe.loading,
      reload: rerun,
      measuredAt: probe.data?.measuredAt ?? null,
      rerun,
    }),
    [probe.data, probe.error, probe.loading, rerun]
  );

  const store = useMemo<HubStore>(() => ({ servers, groups, diagnosis }), [servers, groups, diagnosis]);
  return <HubStoreContext.Provider value={store}>{children}</HubStoreContext.Provider>;
}

/** Shared servers, groups and fleet diagnosis; must be used under {@link HubStoreProvider}. */
export function useHubStore(): HubStore {
  const store = useContext(HubStoreContext);
  if (store === null) throw new Error('useHubStore must be used inside HubStoreProvider');
  return store;
}

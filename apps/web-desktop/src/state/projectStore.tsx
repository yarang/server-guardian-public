import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { fetchProjects } from '../api';
import { useHubData } from '../hooks/useHubData';
import type { HubDataState } from '../hooks/useHubData';
import { DEFAULT_PROJECT, loadStoredProject, storeProject } from '../lib/project';
import type { ProjectList } from '../types';

export interface ProjectStore {
  /** Project the Debate and Approvals pages (and Overview) work on; always a usable id. */
  selected: string;
  /** Choose a project; remembered per browser. */
  select: (id: string) => void;
  /** The hub's project list (shared with Settings, so create / delete updates the switcher). */
  projects: HubDataState<ProjectList>;
}

const ProjectStoreContext = createContext<ProjectStore | null>(null);

/**
 * App-level selected project. The choice is persisted in localStorage; once the project list has
 * loaded, a stored id that no longer exists falls back to DEFAULT_PROJECT (the stored value is left
 * alone, so a temporarily unreachable list never forgets the choice).
 */
export function ProjectStoreProvider({ children }: { children: ReactNode }) {
  const projects = useHubData(fetchProjects);
  const [stored, setStored] = useState(loadStoredProject);

  const known = projects.data?.projects;
  const selected = known && !known.some((p) => p.id === stored) ? DEFAULT_PROJECT : stored;

  const select = useCallback((id: string) => {
    setStored(id);
    storeProject(id);
  }, []);

  const store = useMemo<ProjectStore>(() => ({ selected, select, projects }), [selected, select, projects]);
  return <ProjectStoreContext.Provider value={store}>{children}</ProjectStoreContext.Provider>;
}

/** Selected project and project list; must be used under {@link ProjectStoreProvider}. */
export function useProjectStore(): ProjectStore {
  const store = useContext(ProjectStoreContext);
  if (store === null) throw new Error('useProjectStore must be used inside ProjectStoreProvider');
  return store;
}

/**
 * Kind of a project from the shared list, or undefined while the list is loading, the project is unknown, or no store
 * is mounted (isolated component tests). Unlike {@link useProjectStore} it never throws.
 */
export function useProjectKind(id: string | undefined): string | undefined {
  const store = useContext(ProjectStoreContext);
  if (store === null || id === undefined) return undefined;
  return store.projects.data?.projects.find((p) => p.id === id)?.data.kind;
}

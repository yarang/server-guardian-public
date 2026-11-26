import type { ReactNode } from 'react';
import { ApprovalsStoreProvider, useApprovalsStore } from './approvalsStore';
import { DebateStoreProvider } from './debateStore';
import { HubStoreProvider } from './hubStore';
import { OperationsStoreProvider } from './operationsStore';
import { ProjectStoreProvider } from './projectStore';
import { RunStoreProvider } from './runStore';

/** Operations refresh the approvals list when they settle, so it sits just under the approvals store. */
function Operations({ children }: { children: ReactNode }) {
  const { refresh } = useApprovalsStore();
  return <OperationsStoreProvider onSettled={refresh}>{children}</OperationsStoreProvider>;
}

/** Every app-level store in dependency order (project first: approvals and debates follow its choice). */
export function AppStores({ children }: { children: ReactNode }) {
  return (
    <HubStoreProvider>
      <ProjectStoreProvider>
        <ApprovalsStoreProvider>
          <Operations>
            <DebateStoreProvider>
              <RunStoreProvider>{children}</RunStoreProvider>
            </DebateStoreProvider>
          </Operations>
        </ApprovalsStoreProvider>
      </ProjectStoreProvider>
    </HubStoreProvider>
  );
}

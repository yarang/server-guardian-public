import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Icon, Sidebar, StatusChip, TabBar } from '@server-guardian/ui';
import type { NavItemProps } from '@server-guardian/ui';
import { getAgentUrl, resumeRequests } from './api';
import { tr } from './i18n';
import { LoginDialog } from './components/LoginDialog';
import { SettingsDialog } from './components/SettingsDialog';
import { RefreshContext } from './hooks/refresh';
import { useAgentOnline } from './hooks/useAgentOnline';
import { useNarrow } from './hooks/useNarrow';
import { ApprovalsPage } from './pages/ApprovalsPage';
import { DebatesPage } from './pages/DebatesPage';
import { FleetPage } from './pages/FleetPage';
import { SettingsPage } from './pages/SettingsPage';
import { ROUTES, hrefFor, useRoute } from './router';
import type { ParsedRoute, RouteId } from './router';
import { AppStores } from './state/AppStores';
import { useApprovalsStore } from './state/approvalsStore';
import { useOperationsStore } from './state/operationsStore';
import { useRunStore } from './state/runStore';

interface PageContext {
  params: ParsedRoute['params'];
  query: ParsedRoute['query'];
  agentUrl: string;
  openSettings: () => void;
}

/** Route -> page mapping. */
const PAGES: Record<RouteId, (ctx: PageContext) => ReactNode> = {
  fleet: ({ params, query }) => <FleetPage params={params} query={query} />,
  debates: ({ params }) => <DebatesPage jobId={params[0]} wikiPage={params[1]} extra={params.slice(2)} />,
  approvals: ({ params }) => <ApprovalsPage actionId={params[0]} />,
  settings: ({ params, agentUrl, openSettings }) => (
    <SettingsPage agentUrl={agentUrl} onEditEndpoint={openSettings} section={params[0]} />
  ),
};

export function App() {
  const { route, params, query } = useRoute();
  const narrow = useNarrow();
  const [refreshToken, setRefreshToken] = useState(0);
  const [loginOpen, setLoginOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [agentUrl, setAgentUrlState] = useState(getAgentUrl());

  const bump = useCallback(() => setRefreshToken((n) => n + 1), []);

  // api.ts dispatches this on any 401; the data hooks dispatch it for AuthRequiredError too.
  useEffect(() => {
    const open = () => setLoginOpen(true);
    window.addEventListener('guardian:auth-required', open);
    return () => window.removeEventListener('guardian:auth-required', open);
  }, []);

  return (
    <RefreshContext.Provider value={refreshToken}>
      <AppStores>
        <ConnectedShell
          route={route}
          narrow={narrow}
          agentUrl={agentUrl}
          onRefresh={bump}
          onOpenSettings={() => setSettingsOpen(true)}
        >
          {/* Pages share the stores above, so switching routes does not refetch, re-probe, or stop a run or a watched debate. */}
          <div>{PAGES[route]({ params, query, agentUrl, openSettings: () => setSettingsOpen(true) })}</div>
        </ConnectedShell>
      </AppStores>

      {loginOpen ? (
        <LoginDialog
          onSuccess={() => {
            setLoginOpen(false);
            bump();
          }}
          onChangeAgent={() => {
            resumeRequests();
            setLoginOpen(false);
            setSettingsOpen(true);
          }}
        />
      ) : null}
      {settingsOpen && !loginOpen ? (
        <SettingsDialog
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            setAgentUrlState(getAgentUrl());
            setSettingsOpen(false);
            bump();
          }}
          onSignedOut={() => {
            setSettingsOpen(false);
            setLoginOpen(true);
          }}
        />
      ) : null}
    </RefreshContext.Provider>
  );
}

/** Pending-approvals count and/or "Running" in one badge: "3 · Running" keeps both visible. */
export function badgeOf(waiting: number, running: boolean): { badge: string; badgeTone: 'crit' | 'info' } {
  const parts = [waiting ? String(waiting) : '', running ? tr.common.running : ''].filter(Boolean);
  return { badge: parts.join(' · '), badgeTone: waiting ? 'crit' : 'info' };
}

/** Builds the navigation (with the Run and Approvals badges) from app-level state, then renders the shell. */
function ConnectedShell({
  route,
  children,
  ...shell
}: {
  route: RouteId;
  narrow: boolean;
  agentUrl: string;
  onRefresh: () => void;
  onOpenSettings: () => void;
  children: ReactNode;
}) {
  const { multiRunning } = useRunStore();
  const { waiting } = useApprovalsStore();
  const { execRunning, publishRunning } = useOperationsStore();
  const current = ROUTES.find((r) => r.id === route) ?? ROUTES[0];
  const items: NavEntry[] = ROUTES.map((r) => ({
    label: r.label,
    href: hrefFor(r.id),
    icon: <Icon name={r.icon} />,
    active: r.id === route,
    // A group / fleet run keeps going while other pages are open; say so on the nav item.
    ...(r.id === 'fleet' && multiRunning ? { badge: tr.common.running, badgeTone: 'info' as const } : {}),
    // Actions waiting for a human decision in the selected project (hidden when none or unknown) next to Running:
    // an Execute (Approvals) or Publish (Debates) keeps going on the hub while other pages are open.
    ...(r.id === 'approvals' && (waiting || execRunning) ? badgeOf(waiting, execRunning) : {}),
    ...(r.id === 'debates' && publishRunning ? { badge: tr.common.running, badgeTone: 'info' as const } : {}),
  }));
  return (
    <AppShell title={current.title} items={items} {...shell}>
      {children}
    </AppShell>
  );
}

/** Degraded chip whose reasons show as visible text on hover and keyboard focus (and stay readable to screen readers). */
function DegradedChip({ reasons }: { reasons: string[] }) {
  const [shown, setShown] = useState(false);
  const why = reasons.join('; ');
  return (
    <span
      tabIndex={0}
      title={why}
      aria-label={tr.shell.status.degradedAria(why)}
      style={{ position: 'relative', display: 'inline-flex' }}
      onMouseEnter={() => setShown(true)}
      onMouseLeave={() => setShown(false)}
      onFocus={() => setShown(true)}
      onBlur={() => setShown(false)}
    >
      <StatusChip tone="warn">{tr.shell.status.degraded}</StatusChip>
      {shown ? (
        <span
          role="tooltip"
          style={{
            position: 'absolute',
            top: '100%',
            right: 0,
            marginTop: 'var(--sg-s-2)',
            zIndex: 10,
            width: 'max-content',
            maxWidth: 320,
            padding: 'var(--sg-s-3) var(--sg-s-4)',
            background: 'var(--sg-surface-raised)',
            border: '1px solid var(--sg-border)',
            borderRadius: 'var(--sg-r-md)',
            fontSize: 12,
            lineHeight: 1.4,
            overflowWrap: 'anywhere',
          }}
        >
          {why}
        </span>
      ) : null}
    </span>
  );
}

function AgentStatus() {
  const health = useAgentOnline();
  if (health === null) return <StatusChip tone="idle">{tr.shell.status.checking}</StatusChip>;
  if (health.state === 'signed-out') return <StatusChip tone="idle">{tr.shell.status.signInRequired}</StatusChip>;
  if (health.state === 'degraded') return <DegradedChip reasons={health.reasons} />;
  return <StatusChip tone={health.state === 'online' ? 'ok' : 'crit'}>{health.state === 'online' ? tr.shell.status.online : tr.shell.status.offline}</StatusChip>;
}

type NavEntry = NavItemProps & { label: string };

function AppShell({
  title,
  items,
  narrow,
  agentUrl,
  onRefresh,
  onOpenSettings,
  children,
}: {
  title: string;
  items: NavEntry[];
  narrow: boolean;
  agentUrl: string;
  onRefresh: () => void;
  onOpenSettings: () => void;
  children: ReactNode;
}) {
  const brand = (
    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sg-s-4)' }}>
      <Icon name="shield" size={20} />
      <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.25 }}>
        <span>{tr.shell.brand.name}</span>
        <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--sg-text-muted)', fontFamily: 'var(--sg-font-mono)' }}>
          {tr.shell.brand.sub}
        </span>
      </div>
    </div>
  );
  const footer = (
    <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
      <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.06em', textTransform: 'uppercase' }}>{tr.shell.header.hubLabel}</span>
      <span style={{ fontFamily: 'var(--sg-font-mono)', overflowWrap: 'anywhere' }}>{agentUrl}</span>
    </div>
  );

  return (
    <div style={{ display: 'flex', minHeight: '100vh' }}>
      <div style={{ position: 'sticky', top: 0, alignSelf: 'flex-start', height: '100vh', display: 'flex' }}>
        <Sidebar aria-label={tr.shell.nav.primary} brand={brand} items={items} footer={footer} />
      </div>
      <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <header
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 'var(--sg-s-5)',
            padding: 'var(--sg-s-7) var(--sg-s-10)',
            borderBottom: '1px solid var(--sg-border)',
          }}
        >
          <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700 }}>{title}</h1>
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-4)' }}>
            <AgentStatus />
            <Button size="sm" variant="outline" onClick={onOpenSettings}>
              {tr.shell.header.hubConnection}
            </Button>
            <Button size="sm" variant="secondary" onClick={onRefresh}>
              {tr.shell.header.refresh}
            </Button>
          </div>
        </header>
        <div style={{ flex: 1, padding: 'var(--sg-s-10)', paddingBottom: narrow ? 96 : 'var(--sg-s-10)', minWidth: 0 }}>
          {children}
        </div>
      </main>
      <TabBar aria-label={tr.shell.nav.primaryMobile} items={items} />
    </div>
  );
}

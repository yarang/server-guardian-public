import { TabBar, Icon } from '@server-guardian/ui';

const tabs = (active: string) =>
  [
    { label: 'Fleet', href: '/fleet', icon: <Icon name="server" size={20} /> },
    { label: 'Security', href: '/security', icon: <Icon name="lock" size={20} /> },
    { label: 'Agents', href: '/agents', icon: <Icon name="bolt" size={20} /> },
    { label: 'Debates', href: '/debates', icon: <Icon name="chat" size={20} /> },
    { label: 'Exec', href: '/exec', icon: <Icon name="terminal" size={20} /> },
  ].map((t) => ({ ...t, active: t.label === active }));

export const FleetActive = () => (
  <div style={{ padding: 16, width: 390 }}>
    <TabBar alwaysVisible aria-label="Primary" items={tabs('Fleet')} />
  </div>
);

export const DebatesActive = () => (
  <div style={{ padding: 16, width: 390 }}>
    <TabBar alwaysVisible aria-label="Primary" items={tabs('Debates')} />
  </div>
);

export const ThreeTabs = () => (
  <div style={{ padding: 16, width: 390 }}>
    <TabBar
      alwaysVisible
      aria-label="Primary"
      items={[
        { label: 'Fleet', href: '/fleet', icon: <Icon name="server" size={20} />, active: true },
        { label: 'Debates', href: '/debates', icon: <Icon name="chat" size={20} /> },
        { label: 'Terminal', href: '/terminal', icon: <Icon name="terminal" size={20} /> },
      ]}
    />
  </div>
);

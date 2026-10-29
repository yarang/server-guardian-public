import { NavItem, Icon } from '@server-guardian/ui';

const list: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 4 };

export const States = () => (
  <div style={{ padding: 16, width: 260 }}>
    <nav aria-label="Primary" style={list}>
      <NavItem label="Fleet Overview" href="/fleet" icon={<Icon name="server" />} badge="23/24" active />
      <NavItem label="SSH Security" href="/security" icon={<Icon name="lock" />} badge={86} />
      <NavItem label="Multi-Agent" href="/agents" icon={<Icon name="bolt" />} />
      <NavItem label="Persona & Debate" href="/debates" icon={<Icon name="chat" />} badge={2} />
      <NavItem label="Remote Terminal" href="/terminal" icon={<Icon name="terminal" />} />
    </nav>
  </div>
);

export const Active = () => (
  <div style={{ padding: 16, width: 260 }}>
    <nav aria-label="Primary" style={list}>
      <NavItem label="Remote Terminal" href="/terminal" icon={<Icon name="terminal" />} active />
    </nav>
  </div>
);

export const WithBadge = () => (
  <div style={{ padding: 16, width: 260 }}>
    <nav aria-label="Primary" style={list}>
      <NavItem label="Approvals" href="/approvals" icon={<Icon name="shield" />} badge={3} />
    </nav>
  </div>
);

export const Plain = () => (
  <div style={{ padding: 16, width: 260 }}>
    <nav aria-label="Primary" style={list}>
      <NavItem label="Batch Exec" href="/exec" icon={<Icon name="copy" />} />
    </nav>
  </div>
);

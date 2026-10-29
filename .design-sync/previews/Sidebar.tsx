import { Sidebar, Icon, StatusDot } from '@server-guardian/ui';

const brand = (
  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
    <Icon name="shield" size={20} />
    <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.25 }}>
      <span>Server Guardian</span>
      <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--sg-text-muted)', fontFamily: 'var(--sg-font-mono)' }}>
        hub · site-a-arm1
      </span>
    </div>
  </div>
);

const providerRow: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--sg-font-mono)' };

const footer = (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
    <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.06em', textTransform: 'uppercase' }}>LLM providers</span>
    <span style={providerRow}><StatusDot tone="ok" size={8} label="ready" />claude · ready</span>
    <span style={providerRow}><StatusDot tone="ok" size={8} label="ready" />codex · ready</span>
    <span style={providerRow}><StatusDot tone="warn" size={8} label="slow" />agy · slow</span>
  </div>
);

const items = [
  { label: 'Fleet Overview', href: '/fleet', icon: <Icon name="server" />, badge: '23/24', active: true },
  { label: 'SSH Security', href: '/security', icon: <Icon name="lock" />, badge: 86 },
  { label: 'Multi-Agent', href: '/agents', icon: <Icon name="bolt" /> },
  { label: 'Persona & Debate', href: '/debates', icon: <Icon name="chat" />, badge: 2 },
  { label: 'Batch Exec', href: '/exec', icon: <Icon name="copy" /> },
  { label: 'Remote Terminal', href: '/terminal', icon: <Icon name="terminal" /> },
];

export const FleetConsole = () => (
  <div style={{ padding: 16, width: 264 }}>
    <div style={{ height: 560, display: 'flex' }}>
      <Sidebar aria-label="Primary" brand={brand} items={items} footer={footer} />
    </div>
  </div>
);

export const DebateActive = () => (
  <div style={{ padding: 16, width: 264 }}>
    <div style={{ height: 560, display: 'flex' }}>
      <Sidebar
        aria-label="Primary"
        brand={brand}
        items={items.map((it) => ({ ...it, active: it.label === 'Persona & Debate' }))}
        footer={footer}
      />
    </div>
  </div>
);

export const BrandOnly = () => (
  <div style={{ padding: 16, width: 264 }}>
    <div style={{ height: 420, display: 'flex' }}>
      <Sidebar
        aria-label="Primary"
        brand={brand}
        items={[
          { label: 'Fleet Overview', href: '/fleet', icon: <Icon name="server" />, active: true },
          { label: 'SSH Security', href: '/security', icon: <Icon name="lock" /> },
          { label: 'Remote Terminal', href: '/terminal', icon: <Icon name="terminal" /> },
        ]}
        footer={<span style={{ fontFamily: 'var(--sg-font-mono)' }}>hub site-a-arm1 · v0.1.0</span>}
      />
    </div>
  </div>
);

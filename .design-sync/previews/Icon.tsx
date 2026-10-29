import { Icon } from '@server-guardian/ui';

const NAMES = [
  'search', 'shield', 'server', 'terminal', 'chat', 'bolt', 'check',
  'x', 'chevron-right', 'chevron-left', 'trash', 'lock', 'plus', 'copy',
] as const;

export const AllIcons = () => (
  <div
    style={{
      padding: 16,
      width: 660,
      display: 'grid',
      gridTemplateColumns: 'repeat(7, 1fr)',
      gap: 16,
    }}
  >
    {NAMES.map((n) => (
      <div key={n} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
        <Icon name={n} size={24} />
        <span style={{ fontSize: 11, opacity: 0.7, whiteSpace: 'nowrap' }}>{n}</span>
      </div>
    ))}
  </div>
);

export const Sizes = () => (
  <div style={{ padding: 16, display: 'flex', gap: 20, alignItems: 'flex-end' }}>
    {[12, 16, 20, 24, 32].map((s) => (
      <div key={s} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
        <Icon name="shield" size={s} />
        <span style={{ fontSize: 11, opacity: 0.7 }}>{s}px</span>
      </div>
    ))}
  </div>
);

export const InheritsColor = () => (
  <div style={{ padding: 16, display: 'flex', gap: 20, alignItems: 'center' }}>
    <span style={{ color: 'var(--sg-ok, #3fb950)', display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <Icon name="check" size={20} title="Healthy" /> edge-mini healthy
    </span>
    <span style={{ color: 'var(--sg-crit, #f85149)', display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <Icon name="x" size={20} title="Failed" /> sshd reload failed
    </span>
  </div>
);

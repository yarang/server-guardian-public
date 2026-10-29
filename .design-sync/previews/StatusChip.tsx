import { StatusChip } from '@server-guardian/ui';

const row: React.CSSProperties = { display: 'flex', gap: 10, alignItems: 'center', padding: 16, flexWrap: 'wrap' };

export const DebateStatuses = () => (
  <div style={row}>
    <StatusChip tone="ok">APPROVED</StatusChip>
    <StatusChip tone="warn">DISPUTED</StatusChip>
    <StatusChip tone="info">PENDING_JEV</StatusChip>
    <StatusChip tone="idle">HELD</StatusChip>
    <StatusChip tone="crit">REJECTED</StatusChip>
  </div>
);

export const RiskLevels = () => (
  <div style={row}>
    <StatusChip tone="crit">HIGH</StatusChip>
    <StatusChip tone="warn">MEDIUM</StatusChip>
    <StatusChip tone="ok">LOW</StatusChip>
  </div>
);

export const HostStates = () => (
  <div style={row}>
    <StatusChip tone="ok">ONLINE</StatusChip>
    <StatusChip tone="crit">DISK 91%</StatusChip>
    <StatusChip tone="idle">OFFLINE</StatusChip>
  </div>
);

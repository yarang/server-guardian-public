import { GuardianRoot, Button, StatusChip } from '@server-guardian/ui';

export const AppShell = () => (
  <GuardianRoot style={{ padding: 24, width: 460 }}>
    <h2 style={{ margin: '0 0 6px', fontSize: 20 }}>Server Guardian</h2>
    <p style={{ margin: '0 0 16px', opacity: 0.75 }}>
      3 nodes monitored · hub site-a-arm1
    </p>
    <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
      <Button variant="primary">New debate</Button>
      <StatusChip tone="ok">fleet healthy</StatusChip>
    </div>
  </GuardianRoot>
);

export const IncidentSummary = () => (
  <GuardianRoot style={{ padding: 24, width: 460 }}>
    <h2 style={{ margin: '0 0 6px', fontSize: 20 }}>site-b-ec1</h2>
    <p style={{ margin: '0 0 16px', opacity: 0.75 }}>
      Disk usage on / reached 91% at 04:12 KST.
    </p>
    <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
      <Button variant="secondary">Review actions</Button>
      <StatusChip tone="crit">disk critical</StatusChip>
      <StatusChip tone="warn">1 pending</StatusChip>
    </div>
  </GuardianRoot>
);

import { Panel, Button, StatusChip, StatusDot, SegmentedControl } from '@server-guardian/ui';

const rowStyle = { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0' } as const;

export const Nodes = () => (
  <div style={{ padding: 16, width: 460 }}>
    <Panel
      title="Nodes"
      actions={
        <SegmentedControl
          aria-label="Range"
          defaultValue="1h"
          options={[{ value: '1h', label: '1h' }, { value: '24h', label: '24h' }, { value: '7d', label: '7d' }]}
        />
      }
    >
      <div style={rowStyle}><span>site-a-arm1 (hub)</span><StatusChip tone="ok">online</StatusChip></div>
      <div style={rowStyle}><span>site-b-ec1</span><StatusChip tone="warn">disk 91%</StatusChip></div>
      <div style={rowStyle}><span>edge-mini</span><StatusChip tone="ok">online</StatusChip></div>
    </Panel>
  </div>
);

export const AwaitingDecision = () => (
  <div style={{ padding: 16, width: 460 }}>
    <Panel
      title="Awaiting your decision"
      tone="warn"
      footer={
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <Button variant="ghost">Reject</Button>
          <Button variant="primary">Approve</Button>
        </div>
      }
    >
      <p style={{ margin: 0 }}>
        Judge proposes rotating /var/log/journal on site-b-ec1 to free 4.2 GB.
      </p>
    </Panel>
  </div>
);

export const Tones = () => (
  <div style={{ padding: 16, width: 620, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
    <Panel title="Fleet probe" tone="ok">All 3 nodes responded.</Panel>
    <Panel title="Disk pressure" tone="warn">site-b-ec1 at 91%.</Panel>
    <Panel title="sshd down" tone="crit">edge-mini refused port 22.</Panel>
    <Panel title="Debate running" tone="info">T-0142 · round 2 of 3.</Panel>
  </div>
);

export const NoHeader = () => (
  <div style={{ padding: 16, width: 460 }}>
    <Panel>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <StatusDot tone="ok" /> <span>Last fleet audit passed 12 minutes ago.</span>
      </div>
    </Panel>
  </div>
);

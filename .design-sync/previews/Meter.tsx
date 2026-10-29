import { Meter } from '@server-guardian/ui';

const col: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 12, padding: 16, width: 380 };

export const DiskUsage = () => (
  <div style={col}>
    <Meter label="site-a-arm1 disk" value={42} />
    <Meter label="site-b-ec1 mem" value={74} />
    <Meter label="site-b-ec1 disk" value={91} />
  </div>
);

export const Large = () => (
  <div style={col}>
    <Meter label="disk /" value={91} size="lg" />
    <Meter label="memory" value={38} size="lg" />
  </div>
);

export const CustomThresholds = () => (
  <div style={col}>
    <Meter label="cpu (warn 50 / crit 80)" value={63} thresholds={{ warn: 50, crit: 80 }} />
    <Meter label="cpu (warn 50 / crit 80)" value={84} thresholds={{ warn: 50, crit: 80 }} />
  </div>
);

export const NoValue = () => (
  <div style={col}>
    <Meter value={55} showValue={false} />
    <Meter label="swap" value={12} showValue={false} />
  </div>
);

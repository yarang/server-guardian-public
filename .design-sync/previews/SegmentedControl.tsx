import { SegmentedControl } from '@server-guardian/ui';

export const TimeRange = () => (
  <div style={{ padding: 16 }}>
    <SegmentedControl
      aria-label="Time range"
      defaultValue="1h"
      options={[
        { value: '15m', label: '15m' },
        { value: '1h', label: '1h' },
        { value: '24h', label: '24h' },
        { value: '7d', label: '7d' },
      ]}
    />
  </div>
);

export const NodeFilter = () => (
  <div style={{ padding: 16 }}>
    <SegmentedControl
      aria-label="Node filter"
      defaultValue="warn"
      options={[
        { value: 'all', label: 'All nodes' },
        { value: 'warn', label: 'Warnings' },
        { value: 'crit', label: 'Critical' },
      ]}
    />
  </div>
);

export const TwoOptions = () => (
  <div style={{ padding: 16 }}>
    <SegmentedControl
      aria-label="View"
      value="table"
      options={[
        { value: 'table', label: 'Table' },
        { value: 'heatmap', label: 'Heatmap' },
      ]}
    />
  </div>
);

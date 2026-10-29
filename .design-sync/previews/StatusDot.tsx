import { StatusDot } from '@server-guardian/ui';

const row: React.CSSProperties = { display: 'flex', gap: 24, alignItems: 'center', padding: 16, flexWrap: 'wrap' };
const item: React.CSSProperties = { display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 };

export const Tones = () => (
  <div style={row}>
    <span style={item}><StatusDot tone="ok" size={12} label="online" />ok · online</span>
    <span style={item}><StatusDot tone="warn" size={12} label="degraded" />warn · degraded</span>
    <span style={item}><StatusDot tone="crit" size={12} label="critical" />crit · critical</span>
    <span style={item}><StatusDot tone="info" size={12} label="syncing" />info · syncing</span>
    <span style={item}><StatusDot tone="idle" size={12} label="offline" />idle · offline</span>
  </div>
);

export const FleetHosts = () => (
  <div style={{ ...row, flexDirection: 'column', alignItems: 'flex-start', gap: 12 }}>
    <span style={item}><StatusDot tone="ok" size={10} pulse label="online" />site-a-arm1 (hub)</span>
    <span style={item}><StatusDot tone="crit" size={10} pulse label="disk critical" />site-b-ec1 · disk 91%</span>
    <span style={item}><StatusDot tone="warn" size={10} label="degraded" />site-b-ec1 · SSH score 62</span>
    <span style={item}><StatusDot tone="idle" size={10} label="offline" />edge-mini · offline</span>
  </div>
);

export const Sizes = () => (
  <div style={row}>
    <span style={item}><StatusDot tone="ok" size={8} label="online" />8px</span>
    <span style={item}><StatusDot tone="ok" size={12} label="online" />12px</span>
    <span style={item}><StatusDot tone="ok" size={16} label="online" />16px</span>
    <span style={item}><StatusDot tone="crit" size={16} pulse label="critical" />16px pulse</span>
  </div>
);

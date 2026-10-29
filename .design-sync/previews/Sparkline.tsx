import { Sparkline } from '@server-guardian/ui';

const col: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 14, padding: 16 };
const item: React.CSSProperties = { display: 'flex', gap: 12, alignItems: 'center', fontSize: 13 };
const lbl: React.CSSProperties = { width: 200 };

export const DiskTrend = () => (
  <div style={{ padding: 16 }}>
    <Sparkline tone="crit" width={240} height={48} points={[71, 72, 74, 76, 77, 80, 83, 85, 88, 89, 91]} />
  </div>
);

export const Tones = () => (
  <div style={col}>
    <span style={item}><span style={lbl}>ok · site-a-arm1 cpu</span><Sparkline tone="ok" width={240} height={48} points={[22, 25, 19, 30, 28, 24, 26, 21, 27, 23]} /></span>
    <span style={item}><span style={lbl}>warn · site-b-ec1 mem</span><Sparkline tone="warn" width={240} height={48} points={[60, 63, 66, 65, 70, 72, 71, 74, 73, 74]} /></span>
    <span style={item}><span style={lbl}>crit · site-b-ec1 disk</span><Sparkline tone="crit" width={240} height={48} points={[78, 80, 81, 83, 85, 86, 88, 89, 90, 91]} /></span>
    <span style={item}><span style={lbl}>info · debates / hour</span><Sparkline tone="info" width={240} height={48} points={[2, 4, 1, 3, 6, 5, 2, 7, 4, 3]} /></span>
    <span style={item}><span style={lbl}>idle · edge-mini (offline)</span><Sparkline tone="idle" width={240} height={48} points={[]} /></span>
  </div>
);

export const InlineDefaultSize = () => (
  <div style={{ ...col, flexDirection: 'row', gap: 24 }}>
    <span style={item}>SSH score <Sparkline tone="warn" points={[80, 78, 75, 70, 66, 64, 62]} /></span>
    <span style={item}>load <Sparkline tone="ok" points={[0.4, 0.6, 0.5, 0.9, 0.7, 0.5]} /></span>
  </div>
);

import { StatCard } from '@server-guardian/ui';

export const NodesOnline = () => (
  <div style={{ padding: 16, width: 240 }}>
    <StatCard label="Nodes online" value="11" unit="/ 12" caption="edge-mini offline" tone="crit" />
  </div>
);

export const CaptionTones = () => (
  <div style={{ padding: 16, display: 'grid', gridTemplateColumns: 'repeat(3, 220px)', gap: 12 }}>
    <StatCard label="Fleet health" value="98.2" unit="%" caption="all checks passing" tone="ok" />
    <StatCard label="Worst SSH score" value="62" caption="site-b-ec1" tone="warn" />
    <StatCard label="Peak disk" value="91" unit="%" caption="site-b-ec1 /" tone="crit" />
    <StatCard label="Debates today" value="14" caption="3 PENDING_JEV" tone="info" />
    <StatCard label="Held actions" value="2" caption="awaiting operator" tone="idle" />
  </div>
);

export const AsLink = () => (
  <div style={{ padding: 16, width: 240 }}>
    <StatCard label="Pending approvals" value="3" caption="Review now" tone="info" href="/approvals" />
  </div>
);

export const NoCaption = () => (
  <div style={{ padding: 16, width: 240 }}>
    <StatCard label="Hub uptime" value="41" unit="days" />
  </div>
);

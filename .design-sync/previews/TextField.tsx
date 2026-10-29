import { TextField } from '@server-guardian/ui';

export const Default = () => (
  <div style={{ padding: 16, width: 360 }}>
    <TextField label="Target" defaultValue="site-b-ec1" />
  </div>
);

export const MonoWithHint = () => (
  <div style={{ padding: 16, width: 360 }}>
    <TextField label="Command" mono defaultValue="df -h /" hint="Runs once on the target as the guardian user." />
  </div>
);

export const WithError = () => (
  <div style={{ padding: 16, width: 360 }}>
    <TextField label="Host" mono defaultValue="mini-01" error="Unknown host — not in the fleet inventory." />
  </div>
);

export const Placeholder = () => (
  <div style={{ padding: 16, width: 360 }}>
    <TextField label="Search nodes" placeholder="site-a-arm1, edge-mini…" />
  </div>
);

export const Disabled = () => (
  <div style={{ padding: 16, width: 360 }}>
    <TextField label="Hub" mono disabled defaultValue="site-a-arm1" hint="Hub is fixed for this fleet." />
  </div>
);

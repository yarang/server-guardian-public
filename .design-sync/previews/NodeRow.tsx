import { NodeRow } from '@server-guardian/ui';

// NodeRow renders role="row"; give it the role="table" parent NodeTable uses.
export const HubOnline = () => (
  <div style={{ padding: 16, width: 980 }}>
    <div role="table" aria-label="Fleet nodes">
      <NodeRow name="site-a-arm1" arch="aarch64" tier="T1" isHub status="online" cpu={38} mem={61} disk={54} load={0.84} sshScore={92} />
    </div>
  </div>
);

export const UnderPressure = () => (
  <div style={{ padding: 16, width: 980 }}>
    <div role="table" aria-label="Fleet nodes">
      <NodeRow name="site-b-ec1" arch="x86_64" tier="T2" status="online" cpu={71} mem={83} disk={91} load={1.92} sshScore={62} />
    </div>
  </div>
);

export const Offline = () => (
  <div style={{ padding: 16, width: 980 }}>
    <div role="table" aria-label="Fleet nodes">
      <NodeRow name="edge-mini" arch="arm64" tier="T2" status="offline" />
    </div>
  </div>
);

export const Stacked = () => (
  <div style={{ padding: 16, width: 980 }}>
    <div role="table" aria-label="Fleet nodes">
      <NodeRow name="site-a-arm1" arch="aarch64" tier="T1" isHub status="online" cpu={38} mem={61} disk={54} load={0.84} sshScore={92} />
      <NodeRow name="site-b-ec1" arch="x86_64" tier="T2" status="online" cpu={71} mem={83} disk={91} load={1.92} sshScore={62} />
      <NodeRow name="edge-mini" arch="arm64" tier="T2" status="offline" />
    </div>
  </div>
);

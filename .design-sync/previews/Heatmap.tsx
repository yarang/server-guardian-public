import { Heatmap } from '@server-guardian/ui';
import type { HeatmapCell, HeatmapColumn, HeatmapRow } from '@server-guardian/ui';

const columns: HeatmapColumn[] = [
  { key: 'SEC-SSH-001', label: 'Root login', title: 'SEC-SSH-001: Disable Direct Root Login via SSH' },
  { key: 'SEC-SSH-002', label: 'Password auth', title: 'SEC-SSH-002: Enforce Key-Based Authentication Only' },
  { key: 'SEC-SSH-003', label: 'Pubkey auth', title: 'SEC-SSH-003: Enable Public Key Authentication' },
  { key: 'SEC-SSH-004', label: 'MaxAuthTries', title: 'SEC-SSH-004: Limit Maximum Authentication Attempts' },
  { key: 'SEC-SSH-005', label: 'Port', title: 'SEC-SSH-005: Custom Non-standard SSH Port' },
];
const cells = (s: string) => s.split('') as HeatmapCell[];

const fleet: HeatmapRow[] = [
  { id: 'a1', name: 'site-a-arm1', cells: cells('PPPPI'), score: 95 },
  { id: 'a2', name: 'site-a-ec1', cells: cells('WPPPI'), score: 88 },
  { id: 'b1', name: 'site-b-arm1', cells: cells('PPPWP'), score: 91 },
  { id: 'b2', name: 'site-b-ec1', cells: cells('FFPNP'), score: 40, status: { tone: 'warn', label: 'partial' } },
  { id: 'g1', name: 'cloud-t-ec1', cells: cells('PFPPI'), score: 71 },
  { id: 'm1', name: 'edge-mini', cells: [], unavailable: 'unreachable: ssh: Could not resolve hostname edge-mini: nodename nor servname provided' },
];

export const SshAudit = () => (
  <div style={{ padding: 16, width: 760 }}>
    <Heatmap legend columns={columns} rows={fleet} />
  </div>
);

export const SelectedNode = () => (
  <div style={{ padding: 16, width: 760 }}>
    <Heatmap legend columns={columns} rows={fleet.slice(0, 4)} selectedId="b2" onSelectRow={() => {}} />
  </div>
);

export const SingleHost = () => (
  <div style={{ padding: 16, width: 560 }}>
    <Heatmap
      rowLabel="Host"
      columns={columns}
      rows={[{ id: 'b2', name: 'site-b-ec1', cells: cells('FFPNP'), score: 40 }]}
    />
  </div>
);

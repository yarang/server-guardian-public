import { NodeTable } from '@server-guardian/ui';

export const Fleet = () => (
  <div style={{ padding: 16, width: 980 }}>
    <NodeTable
      groups={[
        {
          name: 'Site A Cluster',
          summary: '3 nodes',
          nodes: [
            { name: 'site-a-arm1', arch: 'aarch64', tier: 'T1', isHub: true, status: 'online', cpu: 38, mem: 61, disk: 54, load: 0.84, sshScore: 92 },
            { name: 'site-a-ec1', arch: 'x86_64', tier: 'T2', status: 'online', cpu: 12, mem: 44, disk: 37, load: 0.11, sshScore: 88 },
          ],
        },
        {
          name: 'Site B Cluster',
          summary: '2 nodes · 1 needs attention',
          nodes: [
            { name: 'site-b-arm1', arch: 'aarch64', tier: 'T1', status: 'online', cpu: 22, mem: 48, disk: 62, load: 0.4, sshScore: 85 },
            { name: 'site-b-ec1', arch: 'x86_64', tier: 'T2', status: 'online', cpu: 71, mem: 83, disk: 91, load: 1.92, sshScore: 62 },
          ],
        },
        {
          name: 'On-Premise Workstation',
          summary: '1 node · offline',
          nodes: [{ name: 'edge-mini', arch: 'arm64', tier: 'T2', status: 'offline' }],
        },
      ]}
    />
  </div>
);

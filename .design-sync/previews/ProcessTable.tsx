import { ProcessTable } from '@server-guardian/ui';

export const HubTop = () => (
  <div style={{ padding: 16, width: 760 }}>
    <ProcessTable
      rows={[
        { pid: 41822, user: 'ubuntu', cpu: 18.4, mem: 6.1, command: 'claude -p --effort medium (debate T-0141 · judge)' },
        { pid: 41790, user: 'ubuntu', cpu: 9.7, mem: 2.3, command: 'python3 guardian.py --debate --thread T-0141' },
        { pid: 1204, user: 'guardian', cpu: 3.2, mem: 1.8, command: '/opt/guardian/agent --with-ui :9870' },
        { pid: 2877, user: 'ubuntu', cpu: 1.1, mem: 0.9, command: 'uv run jev-mcp' },
        { pid: 812, user: 'root', cpu: 0.4, mem: 0.6, command: 'tailscaled' },
      ]}
    />
  </div>
);

export const LongCommand = () => (
  <div style={{ padding: 16, width: 640 }}>
    <ProcessTable
      rows={[
        { pid: 41822, user: 'ubuntu', cpu: 18.4, mem: 6.1, command: 'claude -p --effort medium --output-format stream-json (debate T-0141 · judge · round 3)' },
        { pid: 1204, user: 'guardian', cpu: 3.2, mem: 1.8, command: '/opt/guardian/agent --with-ui :9870' },
      ]}
    />
  </div>
);

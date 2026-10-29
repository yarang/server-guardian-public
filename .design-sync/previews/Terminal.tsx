import { Terminal } from '@server-guardian/ui';

const tabs = ['site-b-ec1', 'site-a-arm1', 'local hub'];

export const Session = () => (
  <div style={{ padding: 16, width: 760 }}>
    <Terminal
      tabs={tabs}
      activeTab={0}
      prompt="ubuntu@site-b-ec1 $"
      lines={[
        { kind: 'prompt', text: 'uptime' },
        { kind: 'out', text: ' 09:58:12 up 41 days, load average: 1.92, 1.74, 1.60' },
        { kind: 'prompt', text: 'sudo du -sh /var/log/journal' },
        { kind: 'out', text: '14G\t/var/log/journal' },
        { kind: 'meta', text: 'exit 0 · 0.42s' },
      ]}
    />
  </div>
);

export const ConnectionError = () => (
  <div style={{ padding: 16, width: 760 }}>
    <Terminal
      tabs={tabs}
      activeTab={2}
      prompt="guardian@hub $"
      lines={[
        { kind: 'prompt', text: 'ssh ubuntu@100.64.0.20 uptime' },
        { kind: 'err', text: 'ssh: connect to host 100.64.0.20 port 22: Connection timed out' },
        { kind: 'meta', text: 'exit 255 · 10.00s' },
      ]}
    />
  </div>
);

export const NoTabs = () => (
  <div style={{ padding: 16, width: 760 }}>
    <Terminal
      prompt="$"
      placeholder="Run a command on site-a-arm1"
      lines={[
        { kind: 'meta', text: 'connected to site-a-arm1 (100.64.0.11) via tailscale' },
        { kind: 'prompt', text: 'df -h /' },
        { kind: 'out', text: 'Filesystem      Size  Used Avail Use% Mounted on' },
        { kind: 'out', text: '/dev/sda1        96G   52G   44G  54% /' },
      ]}
    />
  </div>
);

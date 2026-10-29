import { CommandPalette, Icon } from '@server-guardian/ui';

const scopes = ['All', 'Nodes', 'Actions', 'Debates', 'Commands'];

const nodes = {
  name: 'Nodes',
  items: [
    { icon: <Icon name="server" />, label: 'site-b-ec1', sub: 'Site B Cluster · x86_64 · 100.64.0.19', tag: 'disk 91%', tagTone: 'crit' as const },
    { icon: <Icon name="server" />, label: 'site-b-arm1', sub: 'Site B Cluster · aarch64 · 100.64.0.21', tag: 'ok', tagTone: 'ok' as const },
    { icon: <Icon name="server" />, label: 'site-b-ec2', sub: 'Site B Cluster · x86_64 · 100.64.0.20', tag: 'ok', tagTone: 'ok' as const },
  ],
};
const actions = {
  name: 'Actions on site-b-ec1',
  items: [
    { icon: <Icon name="terminal" />, label: 'Open terminal', shortcut: '⌘T' },
    { icon: <Icon name="lock" />, label: 'Run SSH audit', shortcut: '⌘R' },
    { icon: <Icon name="shield" />, label: 'Review action #57', sub: 'server_remediation · from T-0142', tag: 'HIGH', tagTone: 'crit' as const },
  ],
};
const debates = {
  name: 'Debates',
  items: [
    { icon: <Icon name="chat" />, label: 'T-0141 site-b-ec1 디스크 91% 정리 계획', sub: 'claude · codex · agy', tag: 'running', tagTone: 'info' as const },
  ],
};
const commands = {
  name: 'Commands',
  items: [{ icon: <Icon name="bolt" />, label: 'Batch exec on Site B Cluster', sub: '3 nodes', shortcut: 'G E' }],
};

export const SearchAll = () => (
  <div style={{ padding: 16, width: 640 }}>
    <CommandPalette inline query="site-b" scopes={scopes} activeScope="All" groups={[nodes, actions, debates, commands]} />
  </div>
);

export const NodesScope = () => (
  <div style={{ padding: 16, width: 640 }}>
    <CommandPalette inline query="site-b" scopes={scopes} activeScope="Nodes" groups={[nodes]} selectedIndex={0} />
  </div>
);

export const ActionSelected = () => (
  <div style={{ padding: 16, width: 640 }}>
    <CommandPalette inline query="site-b" scopes={scopes} activeScope="Actions" groups={[actions]} selectedIndex={2} />
  </div>
);

export const NoResults = () => (
  <div style={{ padding: 16, width: 640 }}>
    <CommandPalette inline query="site-b-ec9" scopes={scopes} activeScope="All" groups={[]} />
  </div>
);

import { IconButton, Icon } from '@server-guardian/ui';

export const Variants = () => (
  <div style={{ padding: 16, display: 'flex', gap: 12, alignItems: 'center' }}>
    <IconButton variant="primary" aria-label="New debate" icon={<Icon name="plus" />} />
    <IconButton variant="secondary" aria-label="Open terminal" icon={<Icon name="terminal" />} />
    <IconButton variant="outline" aria-label="Copy command" icon={<Icon name="copy" />} />
    <IconButton variant="ghost" aria-label="Search nodes" icon={<Icon name="search" />} />
    <IconButton variant="danger" aria-label="Remove node" icon={<Icon name="trash" />} />
  </div>
);

export const Toolbar = () => (
  <div style={{ padding: 16, display: 'flex', gap: 4, alignItems: 'center' }}>
    <IconButton aria-label="Previous page" icon={<Icon name="chevron-left" />} />
    <span style={{ fontSize: 13, padding: '0 8px' }}>Nodes 1–3 of 3</span>
    <IconButton aria-label="Next page" icon={<Icon name="chevron-right" />} />
  </div>
);

export const Disabled = () => (
  <div style={{ padding: 16, display: 'flex', gap: 12, alignItems: 'center' }}>
    <IconButton variant="primary" disabled aria-label="Approve action" icon={<Icon name="check" />} />
    <IconButton variant="danger" disabled aria-label="Reject action" icon={<Icon name="x" />} />
  </div>
);

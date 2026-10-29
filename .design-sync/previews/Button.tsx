import { Button, Icon } from '@server-guardian/ui';

export const Variants = () => (
  <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', padding: 16 }}>
    <Button variant="primary">New debate</Button>
    <Button variant="secondary">Run fleet probe</Button>
    <Button variant="outline">Run</Button>
    <Button variant="ghost">Skip</Button>
    <Button variant="danger">Run once</Button>
  </div>
);

export const Sizes = () => (
  <div style={{ display: 'flex', gap: 12, alignItems: 'center', padding: 16 }}>
    <Button size="sm" variant="secondary">Add key</Button>
    <Button size="md" variant="secondary">Re-audit fleet</Button>
    <Button size="lg" variant="primary">Review actions</Button>
  </div>
);

export const WithIcon = () => (
  <div style={{ display: 'flex', gap: 12, padding: 16 }}>
    <Button variant="secondary" icon={<Icon name="terminal" />}>Open terminal</Button>
    <Button variant="primary" icon={<Icon name="shield" />}>Run SSH audit</Button>
  </div>
);

export const States = () => (
  <div style={{ display: 'flex', gap: 12, padding: 16 }}>
    <Button variant="primary" loading>Running…</Button>
    <Button variant="secondary" href="#debates">Open console</Button>
  </div>
);

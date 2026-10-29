import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import * as UI from '../src';
import {
  ActionCard,
  Button,
  CodeBlock,
  CommandPalette,
  ConfirmDialog,
  GuardianRoot,
  Heatmap,
  ICON_NAMES,
  Icon,
  IconButton,
  Meter,
  NavItem,
  NodeRow,
  NodeTable,
  Panel,
  ProcessTable,
  SegmentedControl,
  Sidebar,
  Sparkline,
  StatCard,
  StatusChip,
  StatusDot,
  TabBar,
  Terminal,
  TextField,
  VerdictChain,
  meterTone,
} from '../src';
import type { HeatmapRow } from '../src';

describe('smoke: every component renders with zero props', () => {
  const zero: Array<[string, () => React.ReactElement]> = [
    ['GuardianRoot', () => <GuardianRoot />],
    ['Icon', () => <Icon />],
    ['Button', () => <Button />],
    ['IconButton', () => <IconButton aria-label="Add" />],
    ['StatusDot', () => <StatusDot />],
    ['StatusChip', () => <StatusChip />],
    ['Meter', () => <Meter />],
    ['StatCard', () => <StatCard />],
    ['Panel', () => <Panel />],
    ['NodeRow', () => <NodeRow />],
    ['NodeTable', () => <NodeTable />],
    ['NavItem', () => <NavItem />],
    ['ActionCard', () => <ActionCard />],
    ['VerdictChain', () => <VerdictChain />],
    ['Sidebar', () => <Sidebar />],
    ['TabBar', () => <TabBar />],
    ['CodeBlock', () => <CodeBlock />],
    ['Heatmap', () => <Heatmap />],
    ['Sparkline', () => <Sparkline />],
    ['Terminal', () => <Terminal />],
    ['CommandPalette', () => <CommandPalette />],
    ['ConfirmDialog', () => <ConfirmDialog />],
    ['SegmentedControl', () => <SegmentedControl />],
    ['TextField', () => <TextField />],
    ['ProcessTable', () => <ProcessTable />],
  ];
  it.each(zero)('%s', (_name, make) => {
    const { container } = render(make());
    expect(container.firstChild).toBeTruthy();
  });

  it('exports every documented component', () => {
    for (const n of [
      'GuardianRoot', 'Button', 'IconButton', 'StatusDot', 'StatusChip', 'Meter', 'StatCard', 'Panel',
      'NodeRow', 'NodeTable', 'ActionCard', 'VerdictChain', 'Sidebar', 'NavItem', 'TabBar', 'CodeBlock',
      'Heatmap', 'Sparkline', 'Terminal', 'CommandPalette', 'ConfirmDialog', 'SegmentedControl',
      'TextField', 'ProcessTable', 'Icon',
    ]) {
      expect(typeof (UI as Record<string, unknown>)[n]).toBe('function');
    }
  });
});

describe('GuardianRoot', () => {
  it('applies sg-root and theme attribute', () => {
    render(<GuardianRoot data-testid="r">x</GuardianRoot>);
    const el = screen.getByTestId('r');
    expect(el.className).toContain('sg-root');
    expect(el.getAttribute('data-sg-theme')).toBe('dark');
  });
});

describe('Icon', () => {
  it('renders every named icon and is decorative by default', () => {
    for (const name of ICON_NAMES) {
      const { container, unmount } = render(<Icon name={name} />);
      expect(container.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
      unmount();
    }
  });
  it('exposes a title as an accessible image', () => {
    render(<Icon name="lock" title="Locked" />);
    expect(screen.getByRole('img', { name: 'Locked' })).toBeTruthy();
  });
});

describe('Button / IconButton', () => {
  it('applies variant and size classes and fires onClick', () => {
    const onClick = vi.fn();
    render(<Button variant="primary" size="lg" onClick={onClick}>Approve</Button>);
    const btn = screen.getByRole('button', { name: 'Approve' });
    expect(btn.className).toContain('sg-btn--primary');
    expect(btn.className).toContain('sg-btn--lg');
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
  it('blocks clicks when disabled or loading', () => {
    const onClick = vi.fn();
    const { rerender } = render(<Button disabled onClick={onClick}>A</Button>);
    fireEvent.click(screen.getByRole('button'));
    rerender(<Button loading onClick={onClick}>A</Button>);
    const b = screen.getByRole('button');
    expect(b.getAttribute('aria-busy')).toBe('true');
    expect((b as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(b);
    expect(onClick).not.toHaveBeenCalled();
  });
  it('renders a link when href is set, with icon', () => {
    render(<Button href="/nodes" target="_blank" icon={<Icon name="server" />}>Nodes</Button>);
    const a = screen.getByRole('link', { name: 'Nodes' });
    expect(a.getAttribute('href')).toBe('/nodes');
    expect(a.getAttribute('rel')).toBe('noopener noreferrer');
  });
  it('disabled link drops href and prevents click', () => {
    const onClick = vi.fn();
    render(<Button href="/x" disabled onClick={onClick}>Go</Button>);
    const a = screen.getByText('Go').closest('a')!;
    expect(a.getAttribute('href')).toBeNull();
    expect(a.getAttribute('aria-disabled')).toBe('true');
    fireEvent.click(a);
    expect(onClick).not.toHaveBeenCalled();
  });
  it('enabled link forwards onClick', () => {
    const onClick = vi.fn();
    render(<Button href="/x" onClick={onClick}>Go</Button>);
    fireEvent.click(screen.getByRole('link'));
    expect(onClick).toHaveBeenCalled();
  });
  it('IconButton exposes its aria-label and variant', () => {
    const onClick = vi.fn();
    render(<IconButton aria-label="Delete node" variant="danger" icon={<Icon name="trash" />} onClick={onClick} />);
    const b = screen.getByRole('button', { name: 'Delete node' });
    expect(b.className).toContain('sg-iconbtn--danger');
    fireEvent.click(b);
    expect(onClick).toHaveBeenCalled();
  });
});

describe('StatusDot / StatusChip', () => {
  it('uses tone class, pulse and sr-only label', () => {
    render(<StatusDot tone="ok" pulse size={10} label="online" data-testid="d" />);
    const d = screen.getByTestId('d');
    expect(d.className).toContain('sg-dot--ok');
    expect(d.className).toContain('sg-dot--pulse');
    expect(d.style.width).toBe('10px');
    expect(screen.getByText('online').className).toContain('sg-sr-only');
  });
  it('chip shows children with tone', () => {
    render(<StatusChip tone="crit">DISPUTED</StatusChip>);
    expect(screen.getByText('DISPUTED').className).toContain('sg-chip--crit');
  });
});

describe('Meter', () => {
  it('meterTone follows default thresholds (warn 70, crit 85)', () => {
    expect(meterTone(10)).toBe('ok');
    expect(meterTone(69.9)).toBe('ok');
    expect(meterTone(70)).toBe('warn');
    expect(meterTone(84)).toBe('warn');
    expect(meterTone(85)).toBe('crit');
    expect(meterTone(100)).toBe('crit');
  });
  it.each([
    [40, 'ok'],
    [75, 'warn'],
    [90, 'crit'],
  ])('value %i renders tone %s', (value, tone) => {
    render(<Meter value={value} label="CPU" />);
    const m = screen.getByRole('meter', { name: 'CPU' });
    expect(m.getAttribute('data-tone')).toBe(tone);
    expect(m.getAttribute('aria-valuenow')).toBe(String(value));
    expect(m.querySelector('.sg-meter__fill')?.className).toContain(`sg-meter__fill--${tone}`);
  });
  it('honours custom thresholds, clamping and showValue', () => {
    const { rerender } = render(<Meter value={50} thresholds={{ warn: 40, crit: 60 }} />);
    expect(screen.getByRole('meter').getAttribute('data-tone')).toBe('warn');
    rerender(<Meter value={250} showValue={false} size="lg" />);
    const m = screen.getByRole('meter');
    expect(m.getAttribute('aria-valuenow')).toBe('100');
    expect(m.className).toContain('sg-meter--lg');
    expect(m.querySelector('.sg-meter__value')).toBeNull();
    rerender(<Meter value={Number.NaN} />);
    expect(screen.getByRole('meter').getAttribute('aria-valuenow')).toBe('0');
  });
});

describe('StatCard / Panel', () => {
  it('StatCard renders value, unit, caption tone', () => {
    render(<StatCard label="Nodes online" value="11" unit="/12" caption="1 offline" tone="warn" />);
    expect(screen.getByText('Nodes online')).toBeTruthy();
    expect(screen.getByText('/12')).toBeTruthy();
    expect(screen.getByText('1 offline').className).toContain('sg-stat__caption--warn');
  });
  it('StatCard renders as link when href is given', () => {
    render(<StatCard label="Pending" value={3} href="/approvals" />);
    expect(screen.getByRole('link').getAttribute('href')).toBe('/approvals');
  });
  it('Panel renders title, actions, footer and tone', () => {
    const { container } = render(
      <Panel title="Fleet" actions={<button>Refresh</button>} footer="Updated just now" tone="crit" flush>
        body
      </Panel>,
    );
    expect(screen.getByRole('heading', { name: 'Fleet' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy();
    expect(screen.getByText('Updated just now')).toBeTruthy();
    expect(container.querySelector('section')?.className).toContain('sg-panel--crit');
    expect(screen.getByRole('region', { name: 'Fleet' })).toBeTruthy();
  });
});

describe('NodeRow / NodeTable', () => {
  it('renders hub, tier and metrics for an online node', () => {
    render(<NodeRow name="site-a-arm1" arch="arm64" tier="T1" isHub status="online" cpu={42} mem={71} disk={90} load={0.52} sshScore={92} />);
    expect(screen.getByText('site-a-arm1')).toBeTruthy();
    expect(screen.getByText('HUB')).toBeTruthy();
    expect(screen.getByText('0.52')).toBeTruthy();
    expect(screen.getByRole('meter', { name: 'DISK' }).getAttribute('data-tone')).toBe('crit');
    expect(screen.getByText('92').className).toContain('sg-chip--ok');
  });
  it('shows placeholders for offline nodes and tones ssh score', () => {
    const { rerender } = render(<NodeRow name="site-b-ec1" status="offline" cpu={50} sshScore={55} />);
    expect(screen.queryByRole('meter')).toBeNull();
    expect(screen.getByText('55').className).toContain('sg-chip--crit');
    rerender(<NodeRow status="online" sshScore={65} />);
    expect(screen.getByText('65').className).toContain('sg-chip--warn');
    expect(screen.queryByText('HUB')).toBeNull();
  });
  it('NodeTable renders header and groups', () => {
    render(
      <NodeTable
        groups={[
          { name: 'Oracle Cloud', summary: '2 nodes', nodes: [{ name: 'site-b-ec1' }, { name: 'site-a-arm1', isHub: true }] },
          { name: 'Home lab', nodes: [{ name: 'edge-lab', tier: 'T2' }] },
        ]}
      />,
    );
    expect(screen.getByRole('table', { name: 'Fleet nodes' })).toBeTruthy();
    expect(screen.getAllByRole('columnheader')).toHaveLength(7);
    expect(screen.getByText('Oracle Cloud')).toBeTruthy();
    expect(screen.getByText('edge-lab')).toBeTruthy();
    expect(screen.getByText('2 nodes')).toBeTruthy();
  });
});

describe('ActionCard', () => {
  it('shows approve/reject for PROPOSED and fires handlers', () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(
      <ActionCard id="act-7f3a" tool="ssh.harden" risk="HIGH" status="PROPOSED" title="PasswordAuthentication 비활성화" meta="site-b-ec1" command="sed -i s/yes/no/ sshd_config" warning="jev gate: needs review" onApprove={onApprove} onReject={onReject} />,
    );
    expect(screen.getByText('HIGH RISK').className).toContain('sg-chip--crit');
    expect(screen.getByText('PROPOSED').className).toContain('sg-chip--info');
    expect(screen.getByRole('note').textContent).toContain('jev gate');
    expect(screen.getByText('sed -i s/yes/no/ sshd_config')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    expect(onApprove).toHaveBeenCalled();
    expect(onReject).toHaveBeenCalled();
  });
  it('hides buttons for terminal states', () => {
    render(<ActionCard status="EXECUTED" risk="LOW" />);
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.getByText('EXECUTED').className).toContain('sg-chip--ok');
  });
  it('shows buttons for HELD with warn chip', () => {
    render(<ActionCard status="HELD" risk="MEDIUM" />);
    expect(screen.getByText('HELD').className).toContain('sg-chip--warn');
    expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy();
  });
});

describe('VerdictChain', () => {
  it('renders steps in order with results', () => {
    render(
      <VerdictChain
        steps={[
          { label: 'Expert', result: 'PROPOSED', tone: 'info', detail: 'Disable password auth' },
          { label: 'jev-mcp gate', result: 'PENDING_JEV', tone: 'warn' },
          { label: 'Judge', result: 'APPROVED' },
        ]}
      />,
    );
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect(screen.getByText('PENDING_JEV').className).toContain('sg-chip--warn');
    expect(screen.getByText('APPROVED').className).toContain('sg-chip--idle');
    expect(screen.getByText('Disable password auth')).toBeTruthy();
  });
});

describe('Sidebar / NavItem / TabBar', () => {
  it('Sidebar renders brand, items with aria-current, badge and footer', () => {
    render(
      <Sidebar
        brand="Server Guardian"
        footer="Hub: site-a-arm1"
        items={[
          { label: 'Fleet', href: '/fleet', active: true },
          { label: 'Approvals', href: '/approvals', badge: 3 },
        ]}
      >
        <span>extra</span>
      </Sidebar>,
    );
    expect(screen.getByText('Server Guardian')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Fleet' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: /Approvals/ }).getAttribute('aria-current')).toBeNull();
    expect(screen.getByText('3').className).toContain('sg-nav__badge');
    expect(screen.getByText('Hub: site-a-arm1')).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeTruthy();
  });
  it('NavItem badge accepts text and nodes, supports an info tone and hides empty values', () => {
    render(
      <Sidebar
        items={[
          { label: 'Run', href: '#/run', badge: 'Running', badgeTone: 'info' },
          { label: 'Zero', href: '#/zero', badge: 0 },
          { label: 'Off', href: '#/off', badge: false },
          { label: 'Empty', href: '#/empty', badge: '' },
          { label: 'Node', href: '#/node', badge: <b>new</b> },
        ]}
      />,
    );
    expect(screen.getByText('Running').className).toContain('sg-nav__badge--info');
    expect(screen.getByText('0').className).toContain('sg-nav__badge');
    expect(screen.getByText('0').className).not.toContain('--info');
    expect(screen.getByRole('link', { name: 'Off' }).querySelector('.sg-nav__badge')).toBeNull();
    expect(screen.getByRole('link', { name: 'Empty' }).querySelector('.sg-nav__badge')).toBeNull();
    expect(screen.getByText('new').closest('.sg-nav__badge')).toBeTruthy();
  });
  it('TabBar renders an optional badge', () => {
    render(<TabBar items={[{ label: 'Run', href: '#/run', badge: 2, badgeTone: 'info' }, { label: 'Nodes', href: '#/nodes' }]} />);
    expect(screen.getByText('2').className).toContain('sg-nav__badge--info');
    expect(screen.getByRole('link', { name: 'Nodes' }).querySelector('.sg-nav__badge')).toBeNull();
  });
  it('Sidebar renders headed sections after items', () => {
    render(
      <Sidebar
        sections={[
          { heading: 'Monitor', items: [{ label: 'Overview', href: '#/overview', active: true }] },
          { heading: 'Respond', items: [{ label: 'Debate', href: '#/debate' }] },
        ]}
      />,
    );
    const monitor = screen.getByRole('group', { name: 'Monitor' });
    expect(monitor.textContent).toContain('Overview');
    expect(screen.getByRole('group', { name: 'Respond' }).textContent).toContain('Debate');
    expect(screen.getByRole('link', { name: 'Overview' }).getAttribute('aria-current')).toBe('page');
  });
  it('TabBar renders items and alwaysVisible class', () => {
    const { container, rerender } = render(
      <TabBar items={[{ label: 'Fleet', href: '/fleet', active: true, icon: <Icon name="server" /> }, { label: 'Chat' }]} />,
    );
    expect(container.querySelector('nav')?.className).not.toContain('sg-tabbar--always');
    expect(screen.getByRole('link', { name: 'Fleet' }).getAttribute('aria-current')).toBe('page');
    rerender(<TabBar alwaysVisible />);
    expect(container.querySelector('nav')?.className).toContain('sg-tabbar--always');
  });
});

describe('CodeBlock', () => {
  it('tints diff lines', () => {
    const { container } = render(<CodeBlock diff code={'--- a\n+PermitRootLogin no\n-PermitRootLogin yes\n context'} />);
    expect(container.querySelectorAll('[data-diff="add"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-diff="del"]')).toHaveLength(2);
  });
  it('does not tint when diff is off', () => {
    const { container } = render(<CodeBlock code={'+a\n-b'} />);
    expect(container.querySelectorAll('[data-diff]')).toHaveLength(0);
  });
  it('copies to clipboard and shows feedback', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CodeBlock code="systemctl restart sshd" title="remediation.sh" />);
    fireEvent.click(screen.getByRole('button', { name: /copy/i }));
    expect(writeText).toHaveBeenCalledWith('systemctl restart sshd');
    expect(await screen.findByText('Copied')).toBeTruthy();
  });
  it('survives a rejected clipboard write and can hide the button', async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } });
    const { rerender } = render(<CodeBlock code="x" />);
    fireEvent.click(screen.getByRole('button', { name: /copy/i }));
    await Promise.resolve();
    expect(screen.getByText('Copy')).toBeTruthy();
    rerender(<CodeBlock code="x" copyable={false} />);
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('Heatmap', () => {
  it('renders glyphs for P/W/F so state is not color-only', () => {
    render(
      <Heatmap
        columns={['Root login', 'Password auth', 'Port']}
        rows={[{ name: 'site-b-ec1', cells: ['P', 'W', 'F'], score: 72 }]}
      />,
    );
    expect(screen.getByLabelText('Root login: Pass').textContent).toBe('✓');
    expect(screen.getByLabelText('Password auth: Warn').textContent).toBe('!');
    expect(screen.getByLabelText('Port: Fail').textContent).toBe('✕');
    expect(screen.getByText('72')).toBeTruthy();
    expect(screen.getByRole('rowheader', { name: 'site-b-ec1' })).toBeTruthy();
  });
  it('falls back for missing column labels and score', () => {
    render(<Heatmap rows={[{ name: 'n', cells: ['P'] }]} />);
    expect(screen.getByLabelText('Check 1: Pass')).toBeTruthy();
    expect(screen.getByText('—')).toBeTruthy();
  });
  it('renders the legacy API with the same markup (no buttons, legend or chips)', () => {
    const { container } = render(
      <Heatmap columns={['Root login', 'Port']} rows={[{ name: 'site-a-arm1', cells: ['P', 'W'], score: 98 }]} />,
    );
    expect(container.firstElementChild!.className).toBe('sg-heatmap-wrap');
    expect(container.querySelectorAll('thead th')).toHaveLength(4);
    expect(container.querySelector('tbody th')!.innerHTML).toBe('site-a-arm1');
    expect(container.querySelector('thead th[title]')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByRole('list')).toBeNull();
    expect(container.querySelector('.sg-heatmap__row--selected')).toBeNull();
  });
  it('gives info and not-checked cells their own glyph, tone class and label', () => {
    render(<Heatmap columns={['Port', 'MaxAuthTries']} rows={[{ name: 'n', cells: ['I', 'N'] }]} />);
    const info = screen.getByRole('img', { name: 'Port: Info' });
    const none = screen.getByRole('img', { name: 'MaxAuthTries: Not checked' });
    expect(info.textContent).toBe('i');
    expect(info.className).toContain('sg-heatmap__cell--I');
    expect(none.textContent).toBe('–');
    expect(none.className).toContain('sg-heatmap__cell--N');
  });
  it('collapses an unavailable row into one reason cell with a score dash and a status chip', () => {
    const { container } = render(
      <Heatmap
        columns={['A', 'B', 'C']}
        rows={[
          { name: 'ok', cells: ['P', 'P', 'P'], score: 100, status: { tone: 'warn', label: 'partial' } },
          { name: 'edge-mini', cells: [], score: 50, unavailable: 'unreachable: ssh: Could not resolve hostname' },
        ]}
      />,
    );
    const reason = screen.getByText('unreachable: ssh: Could not resolve hostname');
    expect(reason.tagName).toBe('TD');
    expect(reason.getAttribute('colspan')).toBe('3');
    const row = reason.closest('tr')!;
    expect(within(row).getByText('—')).toBeTruthy();
    expect(within(row).queryByText('50')).toBeNull();
    expect(within(row).queryAllByRole('img')).toHaveLength(0);
    expect(screen.getByText('partial').className).toContain('sg-chip--warn');
    expect(container.querySelectorAll('tbody tr')[0].querySelectorAll('td')).toHaveLength(4);
  });
  it('keeps colspan at least 1 without columns', () => {
    render(<Heatmap rows={[{ name: 'x', cells: [], unavailable: 'error' }]} />);
    expect(screen.getByText('error').getAttribute('colspan')).toBe('1');
  });
  it('makes row headers buttons only with onSelectRow and reports the row id', () => {
    const rows: HeatmapRow[] = [
      { id: 'a1', name: 'site-a-arm1', cells: ['P'] },
      { id: 'b2', name: 'site-b-ec1', cells: ['F'] },
    ];
    const { rerender, container } = render(<Heatmap columns={['Root login']} rows={rows} selectedId="b2" />);
    expect(screen.queryByRole('button')).toBeNull();
    // selectedId alone still highlights
    expect(container.querySelector('.sg-heatmap__row--selected th')!.textContent).toBe('site-b-ec1');

    const onSelect = vi.fn();
    rerender(<Heatmap columns={['Root login']} rows={rows} selectedId="b2" onSelectRow={onSelect} />);
    const a1 = screen.getByRole('button', { name: 'site-a-arm1' });
    const b2 = screen.getByRole('button', { name: 'site-b-ec1' });
    expect(a1.getAttribute('type')).toBe('button');
    expect(a1.getAttribute('aria-pressed')).toBe('false');
    expect(b2.getAttribute('aria-pressed')).toBe('true');
    expect(b2.closest('tr')!.className).toContain('sg-heatmap__row--selected');
    fireEvent.click(a1);
    expect(onSelect).toHaveBeenLastCalledWith('a1');
    // Native buttons are keyboard reachable; Enter/Space activation dispatches click.
    a1.focus();
    expect(document.activeElement).toBe(a1);
    fireEvent.click(b2);
    expect(onSelect).toHaveBeenLastCalledWith('b2');
  });
  it('selects by name when rows have no id', () => {
    const onSelect = vi.fn();
    render(<Heatmap rows={[{ name: 'solo', cells: ['P'] }]} selectedId="solo" onSelectRow={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: 'solo' }));
    expect(onSelect).toHaveBeenCalledWith('solo');
    expect(screen.getByRole('button', { name: 'solo' }).getAttribute('aria-pressed')).toBe('true');
  });
  it('accepts duplicate names when ids are unique without key warnings', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <Heatmap
        rows={[
          { id: 'g1/a1', name: 'site-a-arm1', cells: ['P'] },
          { id: 'g2/a1', name: 'site-a-arm1', cells: ['W'] },
        ]}
      />,
    );
    expect(screen.getAllByRole('rowheader', { name: 'site-a-arm1' })).toHaveLength(2);
    expect(err.mock.calls.some((c) => String(c[0]).includes('same key'))).toBe(false);
    err.mockRestore();
  });
  it('exposes column titles as tooltip and description, headers never wrap', () => {
    render(
      <Heatmap
        columns={[{ label: 'Root login', title: 'SEC-SSH-001: Disable Direct Root Login via SSH', key: 'SEC-SSH-001' }, 'Port']}
        rows={[{ name: 'n', cells: ['F', 'I'] }]}
      />,
    );
    const th = screen.getByRole('columnheader', { name: 'Root login' });
    expect(th.getAttribute('title')).toBe('SEC-SSH-001: Disable Direct Root Login via SSH');
    const described = document.getElementById(th.getAttribute('aria-describedby')!)!;
    expect(described.textContent).toBe('SEC-SSH-001: Disable Direct Root Login via SSH');
    expect(screen.getByRole('columnheader', { name: 'Port' }).hasAttribute('title')).toBe(false);
    expect(screen.getByLabelText('Root login: Fail')).toBeTruthy();
  });
  it('lists only the states that appear in the legend', () => {
    render(
      <Heatmap
        legend
        columns={['A', 'B']}
        rows={[
          { name: 'x', cells: ['P', 'I'] },
          { name: 'y', cells: ['F', 'P'] },
          { name: 'z', cells: ['W', 'W'], unavailable: 'unreachable' },
        ]}
      />,
    );
    const legend = screen.getByRole('list', { name: 'Legend' });
    expect(within(legend).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['✓Pass', '✕Fail', 'iInfo']);
  });
  it('omits an empty legend and supports score label and hiding', () => {
    const { rerender } = render(<Heatmap legend rows={[]} scoreLabel="Points" />);
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.getByRole('columnheader', { name: 'Points' })).toBeTruthy();
    rerender(<Heatmap rows={[{ name: 'n', cells: ['P'], score: 7 }]} showScore={false} />);
    expect(screen.queryByRole('columnheader', { name: 'Score' })).toBeNull();
    expect(screen.queryByText('7')).toBeNull();
  });
});

describe('Sparkline', () => {
  it('is aria-hidden and plots points', () => {
    const { container } = render(<Sparkline points={[1, 3, 2, 5]} tone="warn" width={80} height={20} />);
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('aria-hidden')).toBe('true');
    expect(svg.getAttribute('class')).toContain('sg-spark--warn');
    expect(svg.querySelector('polyline')!.getAttribute('points')!.split(' ')).toHaveLength(4);
  });
  it('draws a flat line for fewer than two points', () => {
    const { container } = render(<Sparkline points={[4]} />);
    expect(container.querySelector('polyline')!.getAttribute('points')!.split(' ')).toHaveLength(2);
  });
});

describe('Terminal', () => {
  it('renders tabs, lines and runs commands', () => {
    const onRun = vi.fn();
    const onTabChange = vi.fn();
    render(
      <Terminal
        tabs={['site-b-ec1', 'site-a-arm1']}
        onTabChange={onTabChange}
        prompt="guardian $"
        lines={[
          { kind: 'prompt', text: 'uptime' },
          { kind: 'out', text: ' 10:00 up 3 days' },
          { kind: 'err', text: 'permission denied' },
          { kind: 'meta', text: 'exit 1' },
        ]}
        onRun={onRun}
      />,
    );
    expect(screen.getByText('permission denied').className).toContain('sg-term__line--err');
    fireEvent.click(screen.getByRole('button', { name: 'site-a-arm1' }));
    expect(onTabChange).toHaveBeenCalledWith(1);
    expect(screen.getByRole('button', { name: 'site-a-arm1' }).getAttribute('aria-pressed')).toBe('true');
    const input = screen.getByLabelText('Command') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '  df -h  ' } });
    fireEvent.submit(input.closest('form')!);
    expect(onRun).toHaveBeenCalledWith('df -h');
    expect(input.value).toBe('');
    fireEvent.submit(input.closest('form')!);
    expect(onRun).toHaveBeenCalledTimes(1);
  });
  it('respects a controlled activeTab', () => {
    render(<Terminal tabs={['a', 'b']} activeTab={1} />);
    expect(screen.getByRole('button', { name: 'b' }).getAttribute('aria-pressed')).toBe('true');
  });
});

describe('CommandPalette', () => {
  const groups = [
    { name: 'Nodes', items: [{ label: 'site-b-ec1', sub: 'T1 online', icon: <Icon name="server" />, shortcut: 'G N' }] },
    { name: 'Actions', items: [{ label: 'Approve act-7f3a', tag: 'HELD', tagTone: 'warn' as const }, { label: 'Open debate' }] },
  ];
  it('renders dialog, combobox and options', () => {
    render(<CommandPalette groups={groups} scopes={['All', 'Nodes']} activeScope="All" selectedIndex={1} inline />);
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeTruthy();
    expect(screen.getByRole('combobox')).toBeTruthy();
    const opts = screen.getAllByRole('option');
    expect(opts).toHaveLength(3);
    expect(opts[1].getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('combobox').getAttribute('aria-activedescendant')).toBe(opts[1].id);
    expect(screen.getByText('HELD').className).toContain('sg-chip--warn');
  });
  it('navigates with arrow keys, selects with Enter, closes with Escape', () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    const onSel = vi.fn();
    render(<CommandPalette groups={groups} onSelect={onSelect} onClose={onClose} onSelectedIndexChange={onSel} inline />);
    const input = screen.getByRole('combobox');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(onSel).toHaveBeenLastCalledWith(0);
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(onSel).toHaveBeenLastCalledWith(2);
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith(groups[1].items[1]);
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
  it('supports typing, hover and click', () => {
    const onQuery = vi.fn();
    const onSelect = vi.fn();
    render(<CommandPalette groups={groups} onQueryChange={onQuery} onSelect={onSelect} inline />);
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'ec1' } });
    expect(onQuery).toHaveBeenCalledWith('ec1');
    const opts = screen.getAllByRole('option');
    fireEvent.mouseEnter(opts[2]);
    expect(opts[2].getAttribute('aria-selected')).toBe('true');
    fireEvent.click(opts[0]);
    expect(onSelect).toHaveBeenCalledWith(groups[0].items[0]);
  });
  it('shows an empty state and ignores arrows with no items', () => {
    render(<CommandPalette inline />);
    expect(screen.getByText('No results')).toBeTruthy();
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' });
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' });
  });
  it('renders nothing when closed, and an overlay that closes on backdrop click', () => {
    const onClose = vi.fn();
    const { container, rerender } = render(<CommandPalette open={false} />);
    expect(container.firstChild).toBeNull();
    rerender(<CommandPalette onClose={onClose} />);
    const overlay = container.querySelector('.sg-overlay')!;
    expect(screen.getByRole('dialog').getAttribute('aria-modal')).toBe('true');
    fireEvent.mouseDown(screen.getByRole('dialog'));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.mouseDown(overlay);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('ConfirmDialog', () => {
  it('HIGH risk disables confirm until the phrase matches', () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog risk="HIGH" confirmText="site-b-ec1" title="Apply to site-b-ec1" onConfirm={onConfirm} inline />);
    const confirm = screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    const input = screen.getByLabelText('Type site-b-ec1 to confirm');
    fireEvent.change(input, { target: { value: 'site-b' } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(input, { target: { value: 'site-b-ec1' } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
  it('non-HIGH risk needs no typing', () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog risk="MEDIUM" onConfirm={onConfirm} inline />);
    expect(screen.queryByRole('textbox')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(onConfirm).toHaveBeenCalled();
  });
  it('fires cancel/reject and Escape, shows description and children', () => {
    const onCancel = vi.fn();
    const onReject = vi.fn();
    render(
      <ConfirmDialog description="Restarts sshd" onCancel={onCancel} onReject={onReject}>
        <code>systemctl restart sshd</code>
      </ConfirmDialog>,
    );
    expect(screen.getByText('Restarts sshd')).toBeTruthy();
    expect(screen.getByText('systemctl restart sshd')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    fireEvent.keyDown(screen.getByRole('alertdialog'), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(onReject).toHaveBeenCalledTimes(1);
  });
  it('modal dialog takes focus and traps Tab', () => {
    render(<ConfirmDialog risk="HIGH" />);
    const dialog = screen.getByRole('alertdialog');
    expect(document.activeElement).toBe(dialog);
    const buttons = screen.getAllByRole('button');
    const last = buttons[buttons.length - 1];
    // Confirm is disabled, so the last focusable is Cancel.
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    cancel.focus();
    fireEvent.keyDown(cancel, { key: 'Tab' });
    expect(document.activeElement).toBe(screen.getByLabelText(/Type CONFIRM/));
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(cancel);
    expect(last).toBeTruthy();
  });
  it('resets typed text when reopened and renders nothing when closed', () => {
    const { rerender, container } = render(<ConfirmDialog risk="HIGH" inline />);
    fireEvent.change(screen.getByLabelText(/Type CONFIRM/), { target: { value: 'CONFIRM' } });
    rerender(<ConfirmDialog risk="HIGH" open={false} inline />);
    expect(container.firstChild).toBeNull();
    rerender(<ConfirmDialog risk="HIGH" open inline />);
    expect((screen.getByLabelText(/Type CONFIRM/) as HTMLInputElement).value).toBe('');
  });
});

describe('SegmentedControl', () => {
  const options = [
    { value: '1h', label: '1h' },
    { value: '24h', label: '24h' },
  ];
  it('calls onChange and toggles aria-pressed (uncontrolled)', () => {
    const onChange = vi.fn();
    render(<SegmentedControl options={options} onChange={onChange} />);
    expect(screen.getByRole('button', { name: '1h' }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: '24h' }));
    expect(onChange).toHaveBeenCalledWith('24h');
    expect(screen.getByRole('button', { name: '24h' }).getAttribute('aria-pressed')).toBe('true');
  });
  it('stays on the controlled value', () => {
    render(<SegmentedControl options={options} value="1h" />);
    fireEvent.click(screen.getByRole('button', { name: '24h' }));
    expect(screen.getByRole('button', { name: '1h' }).getAttribute('aria-pressed')).toBe('true');
  });
});

describe('TextField', () => {
  it('associates a real label with the input', () => {
    render(<TextField label="Host" placeholder="site-b-ec1" mono hint="Hostname or IP" />);
    const input = screen.getByLabelText('Host');
    expect(input.tagName).toBe('INPUT');
    expect(input.className).toContain('sg-field__input--mono');
    expect(input.getAttribute('aria-describedby')).toBeTruthy();
    expect(screen.getByText('Hostname or IP')).toBeTruthy();
  });
  it('marks errors invalid and handles value changes', () => {
    const onChange = vi.fn();
    render(<TextField label="Port" type="number" defaultValue="22" error="Invalid port" onChange={onChange} id="port" />);
    const input = screen.getByLabelText('Port') as HTMLInputElement;
    expect(input.id).toBe('port');
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('Invalid port');
    fireEvent.change(input, { target: { value: '2222' } });
    expect(onChange).toHaveBeenCalled();
  });
});

describe('ProcessTable', () => {
  it('renders rows with formatted numbers', () => {
    render(
      <ProcessTable
        rows={[
          { pid: 1204, user: 'guardian', cpu: 12.345, mem: 3.2, command: '/usr/local/bin/guardian-agent --sse :9870' },
          { pid: '88', user: 'root', cpu: 0, mem: 0.1, command: 'sshd: /usr/sbin/sshd -D' },
        ]}
      />,
    );
    expect(screen.getAllByRole('row')).toHaveLength(3);
    expect(screen.getByText('12.3')).toBeTruthy();
    expect(screen.getByText('/usr/local/bin/guardian-agent --sse :9870')).toBeTruthy();
  });
});

describe('setUiMessages', () => {
  it('replaces built-in texts app-wide while explicit props still win, and resets to English', () => {
    UI.setUiMessages({ copy: '복사', processTable: { user: '사용자' }, terminal: { placeholder: '명령 실행' } });
    const view = render(
      <>
        <CodeBlock code="x" />
        <UI.ProcessTable rows={[]} />
        <UI.Terminal />
        <UI.Terminal placeholder="mine" />
      </>
    );
    expect(screen.getByText('복사')).toBeTruthy();
    expect(screen.getByText('사용자')).toBeTruthy();
    expect(screen.getByText('Command')).toBeTruthy(); // untouched keys of a partially overridden section keep English
    expect(screen.getByPlaceholderText('명령 실행')).toBeTruthy();
    expect(screen.getByPlaceholderText('mine')).toBeTruthy();
    view.unmount();
    UI.setUiMessages({});
    render(<CodeBlock code="x" />);
    expect(screen.getByText('Copy')).toBeTruthy();
  });
});

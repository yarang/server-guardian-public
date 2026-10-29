import type { Tone } from '../types';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { Meter } from './Meter';
import { StatusChip } from './StatusChip';
import { StatusDot } from './StatusDot';

/** Props for {@link NodeRow}. */
export interface NodeRowProps {
  /** Host name, e.g. site-b-ec1. */
  name?: string;
  /** CPU architecture label, e.g. arm64 or amd64. */
  arch?: string;
  /** Agent tier. */
  tier?: 'T1' | 'T2';
  /** Marks the fleet hub (dashboard host). */
  isHub?: boolean;
  /** Connectivity. Offline rows dim the name and hide metrics. */
  status?: 'online' | 'offline';
  /** CPU usage percent (0-100). */
  cpu?: number;
  /** Memory usage percent (0-100). */
  mem?: number;
  /** Disk usage percent (0-100). */
  disk?: number;
  /** 1-minute load average. */
  load?: number;
  /** SSH hardening score (0-100, higher is better). */
  sshScore?: number;
}

function sshTone(score: number): Tone {
  if (score >= 80) return 'ok';
  if (score >= 60) return 'warn';
  return 'crit';
}

/**
 * One fleet table row showing node identity, tier, status, usage meters, load and SSH score.
 */
export function NodeRow({
  name = 'node',
  arch = 'amd64',
  tier = 'T1',
  isHub = false,
  status = 'online',
  cpu,
  mem,
  disk,
  load,
  sshScore,
}: NodeRowProps) {
  const ui = getUiMessages().nodeTable;
  const online = status === 'online';
  const statusText = online ? ui.online : ui.offline;
  const meter = (label: string, v: number | undefined) =>
    online && v !== undefined ? (
      <div role="cell">
        <Meter value={v} label={label} showValue />
      </div>
    ) : (
      <div role="cell" className="sg-noderow__num" aria-label={ui.unavailable(label)}>
        {'—'}
      </div>
    );
  return (
    <div role="row" className={cx('sg-noderow', !online && 'sg-noderow--offline')}>
      <div role="rowheader" className="sg-noderow__node">
        <span className="sg-noderow__name">{name}</span>
        <span className="sg-noderow__arch">{arch}</span>
        <span className="sg-tag">{tier}</span>
        {isHub ? <span className="sg-tag sg-tag--accent">HUB</span> : null}
      </div>
      <div role="cell" className="sg-noderow__status">
        <StatusDot tone={online ? 'ok' : 'crit'} pulse={online} label={statusText} />
        <span aria-hidden="true">{statusText}</span>
      </div>
      {meter('CPU', cpu)}
      {meter('MEM', mem)}
      {meter('DISK', disk)}
      <div role="cell" className="sg-noderow__num">
        {online && load !== undefined ? load.toFixed(2) : '—'}
      </div>
      <div role="cell">
        {sshScore !== undefined ? (
          <StatusChip tone={sshTone(sshScore)} aria-label={ui.sshScore(sshScore)}>
            {sshScore}
          </StatusChip>
        ) : (
          <span className="sg-noderow__num">{'—'}</span>
        )}
      </div>
    </div>
  );
}

/** A named group of nodes (e.g. a cloud region) with an optional summary line. */
export interface NodeGroup {
  name: string;
  summary?: string;
  nodes: NodeRowProps[];
}

/** Props for {@link NodeTable}. */
export interface NodeTableProps {
  /** Node groups; each renders a group header followed by its rows. */
  groups?: NodeGroup[];
  className?: string;
}

/**
 * Fleet table with a column header and grouped NodeRows that scrolls horizontally on narrow screens.
 */
export function NodeTable({ groups = [], className }: NodeTableProps) {
  const ui = getUiMessages().nodeTable;
  return (
    <div className={cx('sg-nodetable', className)}>
      <div role="table" aria-label={ui.label} className="sg-nodetable__inner">
        <div role="row" className="sg-noderow sg-noderow--head">
          <div role="columnheader">{ui.node}</div>
          <div role="columnheader">{ui.status}</div>
          <div role="columnheader">CPU</div>
          <div role="columnheader">MEM</div>
          <div role="columnheader">{ui.disk}</div>
          <div role="columnheader">{ui.load}</div>
          <div role="columnheader">SSH</div>
        </div>
        {groups.map((g) => (
          <div role="rowgroup" key={g.name}>
            <div className="sg-nodegroup">
              <span className="sg-nodegroup__name">{g.name}</span>
              {g.summary ? <span className="sg-nodegroup__summary">{g.summary}</span> : null}
            </div>
            {g.nodes.map((n, i) => (
              <NodeRow key={`${n.name ?? 'node'}-${i}`} {...n} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

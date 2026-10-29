import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';

/** One process entry. */
export interface ProcessRow {
  pid: number | string;
  user: string;
  /** CPU percent. */
  cpu: number;
  /** Memory percent. */
  mem: number;
  command: string;
}

/** Props for {@link ProcessTable}. */
export interface ProcessTableProps {
  rows?: ProcessRow[];
  className?: string;
}

/**
 * Top-style process list with PID, user, CPU, memory and command columns.
 */
export function ProcessTable({ rows = [], className }: ProcessTableProps) {
  const ui = getUiMessages().processTable;
  return (
    <div className={cx('sg-proc-wrap', className)}>
      <table className="sg-proc">
        <thead>
          <tr>
            <th scope="col">PID</th>
            <th scope="col">{ui.user}</th>
            <th scope="col" className="sg-proc__num">
              CPU %
            </th>
            <th scope="col" className="sg-proc__num">
              MEM %
            </th>
            <th scope="col">{ui.command}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.pid}-${i}`}>
              <td>{r.pid}</td>
              <td>{r.user}</td>
              <td className="sg-proc__num">{r.cpu.toFixed(1)}</td>
              <td className="sg-proc__num">{r.mem.toFixed(1)}</td>
              <td className="sg-proc__cmd" title={r.command}>
                {r.command}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

import { useId } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { StatusChip } from './StatusChip';

/**
 * Cell result: Pass, Warn, Fail, Info (informational, e.g. the default port 22) or N (not checked /
 * unknown). Every state has its own glyph and tone so it is never conveyed by color alone.
 */
export type HeatmapCell = 'P' | 'W' | 'F' | 'I' | 'N';

/** A column of a {@link Heatmap}: a short header label with an optional longer description. */
export interface HeatmapColumn {
  /** Short header text, e.g. "Root login". */
  label: string;
  /** Longer description shown as a tooltip and exposed to assistive tech, e.g. "SEC-SSH-001: Disable Direct Root Login via SSH". */
  title?: string;
  /** Stable React key; defaults to the label. */
  key?: string;
}

/** One row of a {@link Heatmap}. */
export interface HeatmapRow {
  /** Row header text, usually the node name. */
  name: string;
  /** Stable row id used as React key and for selection; defaults to the name. */
  id?: string;
  /** One state per column, in column order. Ignored when the row is unavailable. */
  cells: HeatmapCell[];
  /** Aggregate score shown in the last column. */
  score?: number;
  /** Reason the row could not be checked; when set the check columns collapse into one cell showing it and the score shows a dash. */
  unavailable?: string;
  /** Small status chip shown next to the row name, e.g. { tone: 'warn', label: 'partial' }. */
  status?: { tone: Tone; label: string };
}

/** Props for {@link Heatmap}. */
export interface HeatmapProps {
  /** Column labels, one per cell; plain strings or objects with a tooltip title. */
  columns?: Array<string | HeatmapColumn>;
  /** Rows to render, one per checked subject (e.g. node). */
  rows?: HeatmapRow[];
  /** Header text of the first column. */
  rowLabel?: string;
  /** Called with the row id when a row header is activated; makes row headers buttons. */
  onSelectRow?: (id: string) => void;
  /** Id of the highlighted (selected) row. */
  selectedId?: string;
  /** Show a legend of the cell states present in the rows below the table. */
  legend?: boolean;
  /** Header text of the score column. */
  scoreLabel?: string;
  /** Show the score column. */
  showScore?: boolean;
  /** Extra class on the outer scroll wrapper. */
  className?: string;
}

const GLYPH: Record<HeatmapCell, string> = { P: '✓', W: '!', F: '✕', I: 'i', N: '–' };
const ORDER: HeatmapCell[] = ['P', 'W', 'F', 'I', 'N'];

function normalize(c: string | HeatmapColumn): HeatmapColumn {
  return typeof c === 'string' ? { label: c } : c;
}

/**
 * Check matrix (e.g. sshd audit per node) where each cell shows a glyph so state is never color-only.
 * Rows can be unavailable (one reason cell instead of checks) and, with onSelectRow, selectable.
 */
export function Heatmap({
  columns = [],
  rows = [],
  rowLabel,
  onSelectRow,
  selectedId,
  legend = false,
  scoreLabel,
  showScore = true,
  className,
}: HeatmapProps) {
  const uid = useId();
  const ui = getUiMessages().heatmap;
  const WORD = ui.words;
  const cols = columns.map(normalize);
  const present = legend ? ORDER.filter((s) => rows.some((r) => !r.unavailable && r.cells.includes(s))) : [];

  return (
    <div className={cx('sg-heatmap-wrap', className)}>
      <table className="sg-heatmap">
        <thead>
          <tr>
            <th scope="col" style={{ textAlign: 'left' }}>
              {rowLabel ?? ui.rowLabel}
            </th>
            {cols.map((c, i) =>
              c.title ? (
                <th scope="col" key={c.key ?? c.label} title={c.title} aria-describedby={`${uid}-c${i}`}>
                  {c.label}
                  <span id={`${uid}-c${i}`} hidden>
                    {c.title}
                  </span>
                </th>
              ) : (
                <th scope="col" key={c.key ?? c.label}>
                  {c.label}
                </th>
              ),
            )}
            {showScore ? <th scope="col">{scoreLabel ?? ui.scoreLabel}</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const id = r.id ?? r.name;
            const selected = selectedId !== undefined && id === selectedId;
            const plain = !onSelectRow && !r.status;
            return (
              <tr key={id} className={selected ? 'sg-heatmap__row--selected' : undefined}>
                <th scope="row">
                  {plain ? (
                    r.name
                  ) : (
                    <span className="sg-heatmap__rowhead">
                      {onSelectRow ? (
                        <button
                          type="button"
                          className="sg-heatmap__select"
                          aria-pressed={selected}
                          onClick={() => onSelectRow(id)}
                        >
                          {r.name}
                        </button>
                      ) : (
                        r.name
                      )}
                      {r.status ? (
                        <StatusChip tone={r.status.tone} className="sg-heatmap__status">
                          {r.status.label}
                        </StatusChip>
                      ) : null}
                    </span>
                  )}
                </th>
                {r.unavailable !== undefined ? (
                  <td colSpan={Math.max(cols.length, 1)} className="sg-heatmap__unavailable">
                    {r.unavailable}
                  </td>
                ) : (
                  r.cells.map((cell, i) => (
                    <td key={i}>
                      <span
                        className={cx('sg-heatmap__cell', `sg-heatmap__cell--${cell}`)}
                        role="img"
                        aria-label={`${cols[i]?.label ?? ui.check(i + 1)}: ${WORD[cell]}`}
                      >
                        {GLYPH[cell]}
                      </span>
                    </td>
                  ))
                )}
                {showScore ? (
                  <td>
                    <span className="sg-heatmap__score">{r.unavailable !== undefined ? '—' : (r.score ?? '—')}</span>
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
      {present.length ? (
        <ul className="sg-heatmap__legend" aria-label={ui.legend}>
          {present.map((s) => (
            <li key={s} className="sg-heatmap__legend-item">
              <span className={cx('sg-heatmap__cell', `sg-heatmap__cell--${s}`)} aria-hidden="true">
                {GLYPH[s]}
              </span>
              {WORD[s]}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

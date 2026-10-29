import type { CSSProperties } from 'react';
import { tr } from '../i18n';
import { diffLineKind } from '../lib/debate';
import type { DiffLineKind } from '../lib/debate';

const KIND_STYLE: Record<DiffLineKind, CSSProperties> = {
  header: { fontWeight: 700, color: 'var(--sg-text)' },
  hunk: { background: 'var(--sg-info-bg)', color: 'var(--sg-info)' },
  add: { background: 'var(--sg-ok-bg)', color: 'var(--sg-ok)' },
  del: { background: 'var(--sg-crit-bg)', color: 'var(--sg-crit)' },
  ctx: { color: 'var(--sg-text-secondary)' },
};

/**
 * Unified diff, one row per line (+ green, - red, @@ cyan, file headers bold). It scrolls inside
 * its own box. Lines are React text, so markup in a diff is shown, never interpreted.
 */
export function DiffView({ diff, label = tr.shell.diff.label, maxHeight = 360 }: { diff: string; label?: string; maxHeight?: number }) {
  const lines = diff.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n');
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      style={{
        maxWidth: '100%',
        maxHeight,
        overflow: 'auto',
        background: 'var(--sg-code-bg)',
        border: '1px solid var(--sg-border)',
        borderRadius: 'var(--sg-r-md)',
        fontFamily: 'var(--sg-font-mono)',
        fontSize: 12,
        lineHeight: 1.5,
      }}
    >
      {lines.map((line, i) => (
        <div
          key={i}
          data-diff={diffLineKind(line)}
          style={{ whiteSpace: 'pre', minWidth: 'max-content', padding: '0 var(--sg-s-4)', ...KIND_STYLE[diffLineKind(line)] }}
        >
          {line === '' ? ' ' : line}
        </div>
      ))}
    </div>
  );
}

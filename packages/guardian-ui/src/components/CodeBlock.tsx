import { useEffect, useRef, useState } from 'react';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { Icon } from './Icon';

/** Props for {@link CodeBlock}. */
export interface CodeBlockProps {
  /** Source text; newline-separated. */
  code?: string;
  /** Tints lines starting with + (green) and - (red). */
  diff?: boolean;
  /** Optional caption shown in the header bar (e.g. a file name or language). */
  title?: string;
  /** Show the copy-to-clipboard button. */
  copyable?: boolean;
  className?: string;
}

/**
 * Monospace code viewer with optional diff tinting and a copy button.
 */
export function CodeBlock({ code = '', diff = false, title, copyable = true, className }: CodeBlockProps) {
  const ui = getUiMessages();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = async () => {
    try {
      await navigator.clipboard?.writeText(code);
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  const lines = code.split('\n');
  return (
    <div className={cx('sg-code', className)}>
      {title || copyable ? (
        <div className="sg-code__bar">
          <span>{title ?? ''}</span>
          {copyable ? (
            <button type="button" className="sg-btn sg-btn--ghost sg-code__copy" onClick={copy}>
              <Icon name={copied ? 'check' : 'copy'} size={14} />
              <span aria-live="polite">{copied ? ui.copied : ui.copy}</span>
            </button>
          ) : null}
        </div>
      ) : null}
      <pre className="sg-code__pre">
        <code>
          {lines.map((line, i) => {
            const kind = diff ? (line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : undefined) : undefined;
            return (
              <span key={i} className={cx('sg-code__line', kind && `sg-code__line--${kind}`)} data-diff={kind}>
                {line === '' ? ' ' : line}
              </span>
            );
          })}
        </code>
      </pre>
    </div>
  );
}

import type { Tone } from '@server-guardian/ui';

/** Preformatted monospace text box (stdout, messages, old/new text). Text is rendered as a React text node. */
export function MonoBlock({ text, maxHeight = 240, tone }: { text: string; maxHeight?: number; tone?: Tone }) {
  return (
    <pre
      style={{
        margin: 0,
        padding: 'var(--sg-s-4) var(--sg-s-5)',
        maxHeight,
        overflow: 'auto',
        maxWidth: '100%',
        boxSizing: 'border-box',
        background: 'var(--sg-code-bg)',
        border: '1px solid var(--sg-border)',
        borderRadius: 'var(--sg-r-md)',
        color: tone && tone !== 'idle' ? `var(--sg-${tone})` : 'var(--sg-text-secondary)',
        fontFamily: 'var(--sg-font-mono)',
        fontSize: 12,
        lineHeight: 1.5,
        whiteSpace: 'pre-wrap',
        overflowWrap: 'anywhere',
      }}
    >
      {text}
    </pre>
  );
}

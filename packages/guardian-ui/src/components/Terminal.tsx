import { useState } from 'react';
import type { FormEvent } from 'react';
import { cx } from '../utils/cx';
import { useControllable } from '../utils/useControllable';
import { getUiMessages } from '../i18n';

/** One line of terminal output. */
export interface TerminalLine {
  /** prompt: an entered command; out: stdout; err: stderr; meta: system notes. */
  kind: 'prompt' | 'out' | 'err' | 'meta';
  text: string;
}

/** Props for {@link Terminal}. */
export interface TerminalProps {
  /** Session tab labels, e.g. host names. Hidden when empty. */
  tabs?: string[];
  /** Active tab index (controlled). */
  activeTab?: number;
  /** Called when a tab is clicked. */
  onTabChange?: (index: number) => void;
  /** Output lines. */
  lines?: TerminalLine[];
  /** Prompt label shown before commands and the input. */
  prompt?: string;
  /** Input placeholder. */
  placeholder?: string;
  /** Called with the trimmed command when the user presses Enter. */
  onRun?: (command: string) => void;
  className?: string;
}

/**
 * Terminal session view with host tabs, a scrolling log and a command input.
 */
export function Terminal({
  tabs = [],
  activeTab,
  onTabChange,
  lines = [],
  prompt = '$',
  placeholder,
  onRun,
  className,
}: TerminalProps) {
  const [active, setActive] = useControllable<number>(activeTab, 0);
  const [value, setValue] = useState('');
  const ui = getUiMessages().terminal;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const cmd = value.trim();
    if (!cmd) return;
    onRun?.(cmd);
    setValue('');
  };

  return (
    <div className={cx('sg-term', className)}>
      {tabs.length > 0 ? (
        <div className="sg-term__tabs" role="group" aria-label={ui.sessions}>
          {tabs.map((t, i) => (
            <button
              key={`${t}-${i}`}
              type="button"
              className={cx('sg-term__tab', i === active && 'sg-term__tab--active')}
              aria-pressed={i === active}
              onClick={() => {
                setActive(i);
                onTabChange?.(i);
              }}
            >
              {t}
            </button>
          ))}
        </div>
      ) : null}
      <div className="sg-term__log" role="log" aria-label={ui.output} tabIndex={0}>
        {lines.map((l, i) => (
          <p key={i} className={cx('sg-term__line', `sg-term__line--${l.kind}`)}>
            {l.kind === 'prompt' ? <span className="sg-term__ps">{prompt}</span> : null}
            {l.text}
          </p>
        ))}
      </div>
      <form className="sg-term__form" onSubmit={submit}>
        <span className="sg-term__ps" aria-hidden="true">
          {prompt}
        </span>
        <input
          className="sg-term__input"
          type="text"
          aria-label={ui.command}
          placeholder={placeholder ?? ui.placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
        />
      </form>
    </div>
  );
}

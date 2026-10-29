import { useId } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import type { Tone } from '../types';
import { cx } from '../utils/cx';
import { getUiMessages } from '../i18n';
import { useControllable } from '../utils/useControllable';
import { Icon } from './Icon';
import { SegmentedControl } from './SegmentedControl';
import { StatusChip } from './StatusChip';

/** One selectable entry in a {@link CommandPalette}. */
export interface CommandPaletteItem {
  icon?: ReactNode;
  label: string;
  /** Secondary text under the label. */
  sub?: string;
  /** Keyboard shortcut hint, e.g. "G N". */
  shortcut?: string;
  /** Right-aligned status tag text. */
  tag?: string;
  tagTone?: Tone;
}

/** A titled group of palette items. */
export interface CommandPaletteGroup {
  name: string;
  items: CommandPaletteItem[];
}

/** Props for {@link CommandPalette}. */
export interface CommandPaletteProps {
  /** Show the palette. Defaults to true so it renders when used with no props. */
  open?: boolean;
  /** Search text (controlled). */
  query?: string;
  /** Called when the search text changes. */
  onQueryChange?: (query: string) => void;
  /** Scope labels (e.g. All, Nodes, Actions) shown as a segmented control. */
  scopes?: string[];
  activeScope?: string;
  onScopeChange?: (scope: string) => void;
  /** Result groups. Filtering is the caller's responsibility. */
  groups?: CommandPaletteGroup[];
  /** Index of the highlighted item across all groups (controlled). */
  selectedIndex?: number;
  onSelectedIndexChange?: (index: number) => void;
  /** Called when an item is clicked or Enter is pressed. */
  onSelect?: (item: CommandPaletteItem) => void;
  /** Called on Escape or backdrop click. */
  onClose?: () => void;
  /** Input placeholder. */
  placeholder?: string;
  /** Render the dialog in flow without the fixed overlay (for previews). */
  inline?: boolean;
}

/**
 * Searchable command palette dialog with scopes, grouped results and keyboard navigation.
 */
export function CommandPalette({
  open = true,
  query,
  onQueryChange,
  scopes = [],
  activeScope,
  onScopeChange,
  groups = [],
  selectedIndex,
  onSelectedIndexChange,
  onSelect,
  onClose,
  placeholder,
  inline = false,
}: CommandPaletteProps) {
  const uid = useId();
  const ui = getUiMessages().commandPalette;
  const listId = `${uid}-list`;
  const [q, setQ] = useControllable<string>(query, '');
  const [sel, setSel] = useControllable<number>(selectedIndex, 0);

  if (!open) return null;

  const flat = groups.flatMap((g) => g.items);
  const move = (next: number) => {
    if (flat.length === 0) return;
    const clamped = (next + flat.length) % flat.length;
    setSel(clamped);
    onSelectedIndexChange?.(clamped);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      move(sel + 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      move(sel - 1);
    } else if (e.key === 'Enter') {
      const item = flat[sel];
      if (item) onSelect?.(item);
    } else if (e.key === 'Escape') {
      onClose?.();
    }
  };

  let counter = -1;
  const dialog = (
    <div
      className={cx('sg-palette', inline && 'sg-palette--inline')}
      role="dialog"
      aria-modal={inline ? undefined : true}
      aria-label={ui.label}
    >
      <div className="sg-palette__search">
        <Icon name="search" size={18} />
        <input
          className="sg-palette__input"
          type="text"
          role="combobox"
          aria-label={ui.search}
          aria-expanded={true}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={flat.length > 0 ? `${uid}-opt-${Math.min(sel, flat.length - 1)}` : undefined}
          placeholder={placeholder ?? ui.placeholder}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            onQueryChange?.(e.target.value);
          }}
          onKeyDown={onKey}
          autoFocus={!inline}
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      {scopes.length > 0 ? (
        <div className="sg-palette__scopes">
          <SegmentedControl
            aria-label={ui.scope}
            options={scopes.map((s) => ({ value: s, label: s }))}
            value={activeScope}
            onChange={onScopeChange}
          />
        </div>
      ) : null}
      <div className="sg-palette__list" role="listbox" id={listId} aria-label={ui.results}>
        {flat.length === 0 ? <div className="sg-palette__empty">{ui.noResults}</div> : null}
        {groups.map((g) => (
          <div role="group" aria-label={g.name} key={g.name}>
            <div className="sg-palette__group-name" aria-hidden="true">
              {g.name}
            </div>
            {g.items.map((item) => {
              counter += 1;
              const idx = counter;
              return (
                <div
                  key={`${g.name}-${item.label}-${idx}`}
                  id={`${uid}-opt-${idx}`}
                  role="option"
                  aria-selected={idx === sel}
                  className="sg-palette__item"
                  onMouseEnter={() => {
                    setSel(idx);
                    onSelectedIndexChange?.(idx);
                  }}
                  onClick={() => onSelect?.(item)}
                >
                  {item.icon}
                  <span className="sg-palette__text">
                    <span className="sg-palette__label">{item.label}</span>
                    {item.sub ? <span className="sg-palette__sub">{item.sub}</span> : null}
                  </span>
                  {item.tag ? <StatusChip tone={item.tagTone ?? 'idle'}>{item.tag}</StatusChip> : null}
                  {item.shortcut ? <kbd className="sg-palette__kbd">{item.shortcut}</kbd> : null}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );

  if (inline) return dialog;
  return (
    <div
      className="sg-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose?.();
      }}
    >
      {dialog}
    </div>
  );
}

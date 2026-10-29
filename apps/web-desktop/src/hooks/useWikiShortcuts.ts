import { useEffect, useRef } from 'react';

/** Handlers of the wiki viewer's keyboard shortcuts; leave one out to switch that shortcut off. */
export interface WikiShortcuts {
  /** `[` */
  previous?: () => void;
  /** `]` */
  next?: () => void;
  /** `g` then `i` */
  index?: () => void;
  /** `/` */
  find?: () => void;
}

/** How long `g` waits for `i`. */
const SEQUENCE_MS = 1000;

/** True when the key press belongs to something else: a text field, a dialog, or an IME composition. */
function ignored(e: KeyboardEvent): boolean {
  if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return true;
  const el = e.target instanceof HTMLElement ? e.target : null;
  if (el && (el.isContentEditable || /^(input|textarea|select)$/i.test(el.tagName))) return true;
  return document.querySelector('[role="dialog"], [role="alertdialog"], dialog[open]') !== null;
}

/**
 * Keyboard shortcuts of the wiki viewer: `[` and `]` for the previous and next page, `g i` for the index and `/`
 * to find a page. Keys are matched on `code` so they work with the Korean layout too. Nothing fires while a text
 * field has focus, a dialog is open, a modifier key is held or an IME is composing.
 */
export function useWikiShortcuts(handlers: WikiShortcuts): void {
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    let sawG = 0;
    const onKeyDown = (e: KeyboardEvent) => {
      if (ignored(e)) return;
      const now = Date.now();
      const afterG = sawG !== 0 && now - sawG < SEQUENCE_MS;
      sawG = 0;
      const h = latest.current;
      switch (e.code) {
        case 'BracketLeft':
          if (h.previous) {
            e.preventDefault();
            h.previous();
          }
          break;
        case 'BracketRight':
          if (h.next) {
            e.preventDefault();
            h.next();
          }
          break;
        case 'Slash':
          if (h.find && !e.shiftKey) {
            e.preventDefault();
            h.find();
          }
          break;
        case 'KeyG':
          sawG = now;
          break;
        case 'KeyI':
          if (afterG && h.index) {
            e.preventDefault();
            h.index();
          }
          break;
        default:
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);
}

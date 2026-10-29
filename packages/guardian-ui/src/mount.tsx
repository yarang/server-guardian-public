import type { ReactNode } from 'react';
import { createRoot } from 'react-dom/client';

/**
 * Mount a React node into a DOM element, for incremental adoption from vanilla apps.
 *
 * Wrap the node in `GuardianRoot` yourself when you need the base font and colors.
 * Returns a function that unmounts the tree.
 */
export function mount(el: HTMLElement, node: ReactNode): () => void {
  const root = createRoot(el);
  root.render(node);
  return () => root.unmount();
}

/**
 * Follow a hash link the way a browser would: jsdom does not reliably fire `hashchange` for an
 * anchor click, so the link's href is assigned to `location.hash` (which does). Flush timers or
 * `await waitFor` afterwards so useRoute re-renders.
 */
export function follow(link: Element): void {
  const href = link.getAttribute('href');
  if (href === null) throw new Error('follow: element has no href');
  window.location.hash = href;
}

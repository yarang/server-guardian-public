import { useEffect, useState } from 'react';

const QUERY = '(max-width: 759.98px)';

/** True under the design system's 760px breakpoint (where Sidebar hides and TabBar shows). */
export function useNarrow(): boolean {
  const supported = typeof window !== 'undefined' && typeof window.matchMedia === 'function';
  const [narrow, setNarrow] = useState(() => (supported ? window.matchMedia(QUERY).matches : false));
  useEffect(() => {
    if (!supported) return;
    const mq = window.matchMedia(QUERY);
    const onChange = () => setNarrow(mq.matches);
    onChange();
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, [supported]);
  return narrow;
}

import { useEffect, useState } from 'react';
import { ago, fmtDateTime } from '../lib/format';

/** "측정: 3분 전" style text for an epoch-ms time; re-renders every 15 s so it does not go stale. */
export function Ago({ at, prefix = '' }: { at: number; prefix?: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, [at]);
  return (
    <time dateTime={new Date(at).toISOString()} title={fmtDateTime(at)}>
      {prefix}
      {ago(at, now)}
    </time>
  );
}

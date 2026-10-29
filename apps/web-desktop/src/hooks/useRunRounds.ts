import { useEffect, useRef, useState } from 'react';
import { pollDebateEvents } from '../api';

/**
 * Current round of running debates for the run list. Each id keeps an `after` cursor, so a tick
 * only reads events written since the last one. Failures are ignored (the row just shows no round).
 */
export function useRunRounds(ids: string[], skip: string | null, intervalMs = 5000): Record<string, number> {
  const [rounds, setRounds] = useState<Record<string, number>>({});
  const cursors = useRef(new Map<string, number>());
  const key = ids.filter((i) => i !== skip).join(',');

  useEffect(() => {
    const active = key ? key.split(',') : [];
    let alive = true;
    let busy = false;
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        await Promise.all(
          active.map(async (id) => {
            try {
              const r = await pollDebateEvents(id, cursors.current.get(id) ?? 0);
              if (!alive) return;
              cursors.current.set(id, Math.max(cursors.current.get(id) ?? 0, r.next));
              const seen = r.events.reduce((m, e) => (typeof e.round === 'number' ? Math.max(m, e.round) : m), 0);
              if (seen > 0) setRounds((prev) => (prev[id] >= seen ? prev : { ...prev, [id]: seen }));
            } catch {
              // Leave the previous value.
            }
          })
        );
      } finally {
        busy = false;
      }
    };
    void tick();
    const timer = active.length ? window.setInterval(() => void tick(), intervalMs) : undefined;
    return () => {
      alive = false;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [key, intervalMs]);

  return rounds;
}

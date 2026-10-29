import { useEffect, useState } from 'react';
import { checkAgentHealth } from '../api';
import type { AgentHealth } from '../api';
import { useRefreshToken } from './refresh';

/** Agent health: null while the first check is pending. Re-checked every pollMs and on Refresh. */
export function useAgentOnline(pollMs = 5000): AgentHealth | null {
  const refreshToken = useRefreshToken();
  const [health, setHealth] = useState<AgentHealth | null>(null);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      const h = await checkAgentHealth().catch((): AgentHealth => ({ state: 'offline', reasons: [] }));
      if (!cancelled) setHealth(h);
    };
    void check();
    const timer = window.setInterval(() => void check(), pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refreshToken, pollMs]);

  return health;
}

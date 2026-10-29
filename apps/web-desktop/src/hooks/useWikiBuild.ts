import { useEffect, useRef, useState } from 'react';
import { AuthRequiredError, startWikiBuild } from '../api';
import { tr } from '../i18n';
import { errorText } from '../lib/format';
import { runningWikiJob } from '../lib/wiki';
import { useDebateStore } from '../state/debateStore';
import type { DebateJob, WikiBuildRequest } from '../types';

export type WikiBanner = { tone: 'ok' | 'crit'; text: string; job?: string };

export interface WikiBuild {
  /** Starts a build job; the outcome shows in `banner`. */
  build: (req: WikiBuildRequest) => Promise<void>;
  /** The running build job of the project, if any. */
  building: DebateJob | null;
  /** A build is running or being started; further builds wait. */
  busy: boolean;
  banner: WikiBanner | null;
}

/**
 * Starting wiki builds for one project and following the running one. `onEnded` runs when a running build ends, since
 * the page list is computed per call and has to be loaded again.
 */
export function useWikiBuild(project: string, onEnded: () => void): WikiBuild {
  const { recent } = useDebateStore();
  const reloadRecent = recent.reload;
  const [banner, setBanner] = useState<WikiBanner | null>(null);
  const [starting, setStarting] = useState(false);

  const building = runningWikiJob(recent.data, project);
  const buildId = useRef<string | null>(null);
  const ended = useRef(onEnded);
  ended.current = onEnded;
  useEffect(() => {
    if (building) buildId.current = building.id;
    else if (buildId.current !== null) {
      buildId.current = null;
      ended.current();
    }
  }, [building]);

  const build = async (req: WikiBuildRequest) => {
    setStarting(true);
    setBanner(null);
    try {
      const job = await startWikiBuild(project, req);
      setBanner({ tone: 'ok', text: tr.wiki.tab.buildStarted, job: job.id });
      reloadRecent();
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setBanner({ tone: 'crit', text: tr.wiki.tab.buildFailed(errorText(err)) });
    } finally {
      setStarting(false);
    }
  };

  return { build, building, busy: starting || building !== null, banner };
}

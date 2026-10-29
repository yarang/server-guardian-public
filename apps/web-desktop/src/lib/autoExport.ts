import { fetchProjectResults } from '../api';
import type { ResultItem } from '../types';
import { decisionWordStatus } from './debate';
import { exportAutoGet, exportAutoMarks, exportAutoSave, countResults, noCounts } from './exportFolder';
import type { ExportCounts } from './exportFolder';
import { threadFiles } from './exportThread';

/**
 * Automatic export (docs/design/desktop-export.md 3.7). The dashboard decides what is new with a marker per thread
 * (the result's `updated_at`, kept by the shell next to the export root); the shell decides how a file is written.
 */

/** Only a finished debate with a decision is exported on its own: in-progress, failed and cancelled ones are not. */
const AUTO_STATUSES = new Set(['APPROVED', 'DISPUTED']);

/**
 * True when the result should be exported now: it has a decision, something to write, it changed after the switch was
 * turned on (no back-fill) and the shell has not exported this exact `updated_at` yet.
 */
export function isEligible(item: ResultItem, marks: Record<string, string>, sinceMs: number): boolean {
  const word = decisionWordStatus(item.status)?.label;
  if (!word || !AUTO_STATUSES.has(word)) return false;
  if (!item.has_report && item.artifact_versions <= 0) return false;
  const changed = Date.parse(item.updated_at);
  if (!item.updated_at || Number.isNaN(changed) || changed < sinceMs) return false;
  return marks[item.thread_id] !== item.updated_at;
}

export interface AutoRun {
  /** Sum over the results written in this run. */
  counts: ExportCounts;
  /** Results with at least one file written, renamed or updated. */
  changedThreads: number;
  /** Results that could not be exported. They are not retried until their `updated_at` changes. */
  failedThreads: number;
}

export const noRun = (): AutoRun => ({ counts: noCounts(), changedThreads: 0, failedThreads: 0 });

/**
 * One pass over the given projects. `failed` holds `project/thread@marker` of results that already failed, so a
 * result that cannot be exported is not fetched again every tick. Returns null when the switch is off.
 */
export async function runAutoExport(projects: string[], failed: Set<string>): Promise<AutoRun | null> {
  const state = await exportAutoGet();
  if (!state.enabled) return null;

  const run = noRun();
  for (const project of projects) {
    const [results, marks] = await Promise.all([fetchProjectResults(project), exportAutoMarks(project)]);
    for (const item of results.items) {
      if (!isEligible(item, marks, state.sinceMs)) continue;
      const key = `${project}/${item.thread_id}@${item.updated_at}`;
      if (failed.has(key)) continue;
      try {
        const files = await threadFiles(project, item.thread_id, { report: item.has_report, artifacts: item.artifact_versions > 0 });
        const c = countResults(await exportAutoSave(project, item.thread_id, item.updated_at, files));
        run.counts.written += c.written;
        run.counts.skipped += c.skipped;
        run.counts.renamed += c.renamed;
        run.counts.updated += c.updated;
        if (c.written + c.renamed + c.updated > 0) run.changedThreads += 1;
      } catch {
        failed.add(key);
        run.failedThreads += 1;
      }
    }
  }
  return run;
}

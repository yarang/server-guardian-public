import { fetchArtifactManifest, fetchArtifactText, fetchArtifactVersions, fetchThreadReport, fetchThreadReportAsset } from '../api';
import type { ThreadReport } from '../types';
import { artifactView } from './artifacts';
import { countResults, exportSave, noCounts } from './exportFolder';
import type { ExportCounts, ExportFile } from './exportFolder';
import { errorText } from './format';
import { buildStandaloneMarkdown } from './report';

/**
 * What a thread contributes to the export folder (docs/design/desktop-export.md 2):
 * `report.md` (the same standalone text the download button builds) and `v<n>/<file>` per stored artifact version.
 */

const loadAsset = (thread: string) => (name: string) => fetchThreadReportAsset(thread, name).then((a) => a.text);

export async function reportFile(report: ThreadReport, thread: string): Promise<ExportFile> {
  return { relPath: 'report.md', contents: await buildStandaloneMarkdown(report.markdown, thread, loadAsset(thread)) };
}

/** The files of one stored artifact version. Expired files and kinds the hub does not show are left out. */
export async function versionFiles(thread: string, version: number): Promise<{ project: string; files: ExportFile[] }> {
  const manifest = await fetchArtifactManifest(thread, version);
  const files: ExportFile[] = [];
  if (!manifest.expired_at) {
    for (const f of manifest.files) {
      if (f.expired || !artifactView(f)) continue;
      files.push({ relPath: `v${version}/${f.path}`, contents: await fetchArtifactText(thread, version, f.path) });
    }
  }
  return { project: manifest.project, files };
}

export interface ThreadWant {
  report: boolean;
  artifacts: boolean;
}

/** The files of the wanted parts of one thread. `project` is the project the caller is looking at: a thread of another one is refused. */
export async function threadFiles(project: string, thread: string, want: ThreadWant): Promise<ExportFile[]> {
  const files: ExportFile[] = [];
  if (want.report) {
    const report = await fetchThreadReport(thread);
    if (report.project && report.project !== project) throw new Error(`thread ${thread} belongs to another project`);
    files.push(await reportFile(report, thread));
  }
  if (want.artifacts) {
    const list = await fetchArtifactVersions(thread);
    if (list.project && list.project !== project) throw new Error(`thread ${thread} belongs to another project`);
    for (const v of list.versions) {
      if (!v.expired_at) files.push(...(await versionFiles(thread, v.version)).files);
    }
  }
  return files;
}

/** Writes the wanted parts of one thread, never overwriting (manual save). */
export async function exportThread(project: string, thread: string, want: ThreadWant): Promise<ExportCounts> {
  const files = await threadFiles(project, thread, want);
  return files.length === 0 ? noCounts() : countResults(await exportSave(project, thread, files));
}

export interface ProjectExport extends ExportCounts {
  threads: number;
  failed: Array<{ thread: string; message: string }>;
}

/**
 * Writes every listed thread of a project, one call each. A failing thread is recorded and the rest go on, so one
 * oversized or malformed result does not block the others; what is already in the folder is skipped by Rust.
 */
export async function exportProject(
  project: string,
  items: Array<{ thread: string } & ThreadWant>,
  progress: (done: number, total: number) => void,
): Promise<ProjectExport> {
  const total: ProjectExport = { ...noCounts(), threads: 0, failed: [] };
  progress(0, items.length);
  for (const [i, item] of items.entries()) {
    try {
      const c = await exportThread(project, item.thread, item);
      total.written += c.written;
      total.skipped += c.skipped;
      total.renamed += c.renamed;
      total.updated += c.updated;
      total.threads += 1;
    } catch (e) {
      total.failed.push({ thread: item.thread, message: errorText(e) });
    }
    progress(i + 1, items.length);
  }
  return total;
}

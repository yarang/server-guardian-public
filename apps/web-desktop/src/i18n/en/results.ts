// English copy: project results tab. Rules: see ../ko.ts.
import type { Messages } from '../types';

const plural = (n: number) => (n === 1 ? '' : 's');

export const results: Messages['results'] = {
  listTitle: (n) => (n === null ? 'Results' : `Results (${n})`),
  listWhat: 'result list',
  listAria: 'Project results',
  empty: 'No results yet. When a debate finishes its report appears here, and a design project also shows its artifacts.',
  capped: (shown, total) => `Showing the latest ${shown} of ${total}.`,
  openThread: (title) => `Open result ${title}`,
  backToList: '← Back to the result list',
  rounds: (n) => `${n} round${plural(n)}`,
  artifactCount: (versions, files) => `${versions} artifact version${plural(versions)} · ${files} file${plural(files)}`,
  noReport: 'No report',
  threadId: (id) => `Debate ${id}`,
  openArtifacts: 'View artifacts',
  openArtifactsTitle: (thread) => `Open the documents and diagrams debate ${thread} left`,
  detailTitle: (title) => `Result: ${title}`,
  reportMissing: 'This debate has no report, only artifacts.',
};

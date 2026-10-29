import type { Tone } from '@server-guardian/ui';
import { tr } from '../i18n';
import { hrefFor } from '../router';
import type { DebateJob, WikiAuthor, WikiPageSummary, WikiState } from '../types';

// Helpers of the code wiki views (docs/design/debate-code-index-wiki.md, "구현 메모 (P4)").

/** Label of a code wiki build job in the run list and monitor. */
export const WIKI_JOB_LABEL = tr.wiki.jobLabel;

/** Same pattern as the hub and code_wiki.py. */
export const WIKI_PAGE_ID = /^[a-z0-9][a-z0-9-]{0,63}$/;

/** The hub refuses page texts over this many UTF-8 bytes (413). */
export const WIKI_TEXT_LIMIT = 256 * 1024;

export function isWikiJob(job: Pick<DebateJob, 'kind'> | null | undefined): boolean {
  return job?.kind === 'code_wiki';
}

export function wikiStateTone(state: string): Tone {
  switch (state) {
    case 'fresh':
      return 'ok';
    case 'stale':
      return 'warn';
    case 'broken':
      return 'crit';
    default:
      return 'idle';
  }
}

export function wikiStateLabel(state: WikiState | string): string {
  return tr.wiki.stateLabel[state] ?? tr.wiki.stateLabel[state || 'unknown'] ?? state;
}

/** "claude (LLM)" / "alice (사람)" */
export function authorText(a: WikiAuthor | undefined): string {
  if (!a) return tr.wiki.author.unknown;
  if (a.kind === 'human') return tr.wiki.author.person(a.user || tr.wiki.author.unknown);
  if (a.kind === 'llm') return tr.wiki.author.llm(a.provider || 'LLM');
  return a.kind || tr.wiki.author.unknown;
}

export function isHumanAuthor(a: WikiAuthor | undefined): boolean {
  return a?.kind === 'human';
}

/** One scope entry per line. */
export function formatScope(scope: string[]): string {
  return scope.join('\n');
}

export function parseScope(text: string): string[] {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);
}

/** UTF-8 size of a page text, as the hub counts it. */
export function textBytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** Counts of stale and broken pages, for the "rewrite stale" button. */
export function staleCount(pages: WikiPageSummary[]): number {
  return pages.filter((p) => p.state !== 'fresh').length;
}

/** A running wiki build job of the project, if any. */
export function runningWikiJob(jobs: DebateJob[] | null | undefined, project: string): DebateJob | null {
  return (jobs ?? []).find((j) => isWikiJob(j) && j.status === 'running' && (j.project || '') === project) ?? null;
}

/** Where a file view opens: a line or range (`lines`) or a declaration (`symbol`). */
export interface FileLocation {
  lines?: string;
  symbol?: string;
}

/** `#/debates/wiki?file=<path>[&lines=N-M|&symbol=Name]`: the file view of the wiki tab. */
export function fileHref(path: string, at: FileLocation = {}): string {
  const query: Record<string, string> = { file: path };
  if (at.lines) query.lines = at.lines;
  else if (at.symbol) query.symbol = at.symbol;
  return hrefFor('debates', ['wiki'], query);
}

/** `#/debates/wiki?view=files`: the file list without a selected file. */
export const FILES_HREF = hrefFor('debates', ['wiki'], { view: 'files' });

/** `#/debates/wiki?view=history`: the build history and the actions that start a build. */
export const HISTORY_HREF = hrefFor('debates', ['wiki'], { view: 'history' });

/** The three modes of the wiki tab: reading pages, browsing repository files, build history and actions. */
export type WikiMode = 'read' | 'files' | 'history';

/** A page id always means reading; otherwise `?view=history`, or `?view=files` / `?file=` for the file side. */
export function wikiMode(page: string | undefined, query: Record<string, string>): WikiMode {
  if (page) return 'read';
  if (query.view === 'history') return 'history';
  return query.view === 'files' || !!query.file ? 'files' : 'read';
}

/** Build jobs of one project, newest first (the order of `started_at`, falling back to the list order). */
export function wikiJobs(jobs: readonly DebateJob[] | null | undefined, project: string): DebateJob[] {
  const time = (j: DebateJob) => {
    const t = Date.parse(j.started_at);
    return Number.isNaN(t) ? 0 : t;
  };
  return (jobs ?? [])
    .filter((j) => isWikiJob(j) && (j.project || '') === project)
    .map((job, i) => ({ job, i }))
    .sort((a, b) => time(b.job) - time(a.job) || a.i - b.i)
    .map((x) => x.job);
}

/** Parses `N` or `N-M` (1-based, N <= M); null for anything else. */
export function parseLineRange(text: string | undefined): { start: number; end: number } | null {
  const m = /^(\d{1,7})(?:-(\d{1,7}))?$/.exec(text ?? '');
  if (!m) return null;
  const start = Number(m[1]);
  const end = m[2] !== undefined ? Number(m[2]) : start;
  return start >= 1 && end >= start ? { start, end } : null;
}

// --- Viewer: tree, index order, neighbours, search (docs/design/wiki-viewer.md) ---

/** Same separator as `GROUP_SEPARATOR` in code_wiki.py: a group page id is `<overview id>--<name>`. */
const GROUP_SEPARATOR = '--';

/** A page shown at the top of the tree with the group pages that belong to it. */
export interface WikiNode {
  page: WikiPageSummary;
  groups: WikiPageSummary[];
}

export interface WikiTree {
  /** `dir` and `overview` pages (a page without `kind` is a `dir`), plus groups whose overview is missing. */
  roots: WikiNode[];
  /** `feature-*` design pages: read for intent, they have no parent. */
  features: WikiPageSummary[];
}

/**
 * Id of the overview page a group belongs to, or null. Only a `group` has a parent: a hand-made `dir` page may
 * contain `--` in its id (the page id pattern allows it), so the id alone must not decide. When several prefixes
 * are known pages the longest wins.
 */
export function groupParentId(page: Pick<WikiPageSummary, 'id' | 'kind'>, known: ReadonlySet<string>): string | null {
  if (page.kind !== 'group') return null;
  let at = page.id.lastIndexOf(GROUP_SEPARATOR);
  while (at > 0) {
    const parent = page.id.slice(0, at);
    if (known.has(parent) && parent !== page.id) return parent;
    at = page.id.lastIndexOf(GROUP_SEPARATOR, at - 1);
  }
  return null;
}

/** Groups the list into roots (with their groups) and features, keeping the order of the list inside each part. */
export function buildWikiTree(pages: readonly WikiPageSummary[]): WikiTree {
  const known = new Set(pages.map((p) => p.id));
  const nodes = new Map<string, WikiNode>();
  const roots: WikiNode[] = [];
  const features: WikiPageSummary[] = [];
  const grouped: Array<[string, WikiPageSummary]> = [];
  for (const page of pages) {
    const parent = groupParentId(page, known);
    if (parent !== null) grouped.push([parent, page]);
    else if (page.kind === 'feature') features.push(page);
    else {
      const node: WikiNode = { page, groups: [] };
      nodes.set(page.id, node);
      roots.push(node);
    }
  }
  for (const [parent, page] of grouped) {
    const node = nodes.get(parent);
    if (node) node.groups.push(page);
    // The parent exists but is not a root (a feature or itself a group): show the group on its own.
    else roots.push({ page, groups: [] });
  }
  return { roots, features };
}

/** Reading order of the whole wiki: each root followed by its groups, then the feature pages. Every page appears once. */
export function indexOrder(tree: WikiTree): WikiPageSummary[] {
  return [...tree.roots.flatMap((n) => [n.page, ...n.groups]), ...tree.features];
}

/** Previous and next page in `order`; null at the ends or for an unknown id. */
export function pageNeighbours(
  order: readonly WikiPageSummary[],
  id: string
): { prev: WikiPageSummary | null; next: WikiPageSummary | null } {
  const at = order.findIndex((p) => p.id === id);
  if (at < 0) return { prev: null, next: null };
  return { prev: order[at - 1] ?? null, next: order[at + 1] ?? null };
}

/** The overview page of a group in the tree, if it has one. */
export function parentOf(tree: WikiTree, id: string): WikiPageSummary | null {
  return tree.roots.find((n) => n.groups.some((g) => g.id === id))?.page ?? null;
}

/** Pages whose title, id or scope contains `query` (case-insensitive); an empty query matches everything. */
export function filterPages(pages: readonly WikiPageSummary[], query: string): WikiPageSummary[] {
  const q = query.trim().toLowerCase();
  if (!q) return [...pages];
  return pages.filter((p) => p.title.toLowerCase().includes(q) || p.id.includes(q) || p.scope.some((s) => s.toLowerCase().includes(q)));
}

/** Pages that are not fresh, so the reader knows what not to trust before reading. */
export function attentionPages(pages: readonly WikiPageSummary[]): WikiPageSummary[] {
  return pages.filter((p) => p.state !== 'fresh');
}

/** Pages whose narrative links to `id`. Empty when the hub does not send `links`. */
export function referencedBy(pages: readonly WikiPageSummary[], id: string): WikiPageSummary[] {
  return pages.filter((p) => p.id !== id && p.links?.includes(id));
}

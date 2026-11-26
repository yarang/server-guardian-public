import { tr } from '../../i18n';
import type { ReviewListItem, ReviewRequest, ReviewResult } from '../../types';

/**
 * Branch mode of the quick review form (V2a of docs/design/verification-layer.md): shape checks and the display
 * names of the four change sources. Lives here, next to the tab, because the shared `lib/review.ts` belongs to
 * every caller of the V1 helpers. The hub re-checks everything; these checks only catch typos early.
 */

/** A ref expression as the CLI's `validate_ref` sees it (letters, digits and `._/~^-`, 1-100 chars). */
const REF = /^[A-Za-z0-9._/~^-]{1,100}$/;

/** A branch or base worth sending: the pattern above, no leading dash or dot, no `..`, no `@{`. */
export function validBranch(text: string): boolean {
  const t = text.trim();
  return REF.test(t) && !t.startsWith('-') && !t.startsWith('.') && !t.includes('..') && !t.includes('@{') && t !== '@';
}

/** What a list row calls a review: the V2 source first (branch, action), then the V1 range / patch-on-head. */
export function reviewListName(item: ReviewListItem): string {
  const R = tr.review;
  if (item.source_kind === 'ref' && item.ref) return R.branchName(item.ref);
  if (item.source_kind === 'action' && item.action_id !== undefined) return R.actionName(item.action_id);
  if (item.range) return item.range;
  if (item.patch) return R.patchOn((item.head ?? '').slice(0, 12));
  return item.id;
}

/** The change identity of a finished review: the V2 `source` first, the V1 `change` fields after. */
export function reviewChangeName(result: Pick<ReviewResult, 'source' | 'change' | 'base'>): string {
  const R = tr.review;
  if (result.source?.kind === 'ref' && result.source.ref) return R.branchName(result.source.ref);
  if (result.source?.kind === 'action' && result.source.action_id !== undefined) return R.actionName(result.source.action_id);
  if (result.change.range) return R.change(result.change.range);
  return R.patchOn((result.base ?? '').slice(0, 12));
}

/**
 * The change identity while the review still runs, from the stored request (no shas in it yet); null when the
 * request says nothing displayable (an old record, or a patch whose base is unknown until the result).
 */
export function requestChangeName(source: ReviewRequest['source'] | undefined): string | null {
  if (!source) return null;
  const R = tr.review;
  if (source.kind === 'range' && source.range) return source.range;
  if (source.kind === 'ref' && source.ref) return R.branchName(source.ref);
  if (source.kind === 'action' && source.action_id !== undefined) return R.actionName(source.action_id);
  if (source.kind === 'patch') return R.patchName;
  return null;
}

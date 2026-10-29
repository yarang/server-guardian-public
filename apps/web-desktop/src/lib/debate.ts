import type { Tone } from '@server-guardian/ui';
import { HubError } from '../api';
import { tr } from '../i18n';
import { errorText, fmtDateTime } from './format';
import type { RunState } from './agentMonitor';
import type {
  ActionItem,
  DebateAction,
  DebateFinalDecision,
  DebateJob,
  DebateResult,
  DebateTranscriptItem,
  PlanItem,
  TeamData,
  TeamEntry,
  TeamFileData,
  TeamMemberData,
  TeamMemberRaw,
  CodeTools,
} from '../types';

// --- Validation shared by the console and the team editor -----------------------------------

export const TEAM_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
export const THREAD_ID_RE = /^thread_[A-Za-z0-9_]{1,80}$/;
export const CONTEXT_MAX_PATHS = 20;
export const CONTEXT_MAX_LEN = 200;
export const TASK_MAX = 4000;

/** One non-empty trimmed entry per line. */
export function parseGlobLines(text: string): string[] {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l !== '');
}

/** Error message for an unacceptable reference-code list, or '' when it is fine. */
export function checkGlobs(paths: string[]): string {
  if (paths.length > CONTEXT_MAX_PATHS) return tr.debate.lib.globsTooMany(CONTEXT_MAX_PATHS, paths.length);
  for (const p of paths) {
    if (p.length > CONTEXT_MAX_LEN) return tr.debate.lib.globTooLong(CONTEXT_MAX_LEN);
    if (p.startsWith('-')) return tr.debate.lib.globDash(p.slice(0, 40));
  }
  return '';
}

// --- Team data ---------------------------------------------------------------------------------

/** Member keys the editor owns; everything else on a stored member (`host`, ...) is carried through untouched. */
const MEMBER_KEYS = ['persona', 'providers', 'code_tools', 'host'];
/** Team keys the editor owns or the hub stamps; everything else (`flow`, `sides`, ...) is carried through untouched. */
const TEAM_KEYS = ['id', 'name', 'expert', 'proposers', 'critic', 'judge', 'max_rounds', 'allowed_tools', 'context', 'category', 'created', 'modified', 'source'];

function unknownKeys(raw: object, owned: string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(raw).filter(([k]) => !owned.includes(k)));
}

export interface Member {
  persona: string;
  providers: string[];
  /** `mcp` or undefined (default: repo blocks). */
  codeTools?: CodeTools;
  /** Placement: a label (`worker:any`) or a worker host id; undefined runs on the hub. */
  host?: string;
  /** Stored keys this editor does not model; saving writes them back, so an edit never drops them. */
  extra?: Record<string, unknown>;
}

export function normMember(raw: TeamMemberRaw | undefined): Member {
  if (typeof raw === 'string') return { persona: raw, providers: [] };
  if (raw && typeof raw === 'object') {
    const m: Member = { persona: String(raw.persona || ''), providers: Array.isArray(raw.providers) ? raw.providers.map(String) : [] };
    if (raw.code_tools === 'mcp') m.codeTools = 'mcp';
    if (typeof raw.host === 'string' && raw.host) m.host = raw.host;
    const extra = unknownKeys(raw, MEMBER_KEYS);
    if (Object.keys(extra).length) m.extra = extra;
    return m;
  }
  return { persona: '', providers: [] };
}

/** Member in the saved shape: `code_tools` and `host` only when set, unmodelled keys as they were stored. */
export function memberData(m: Member): TeamMemberData & { persona: string; providers: string[] } {
  const out: TeamMemberData & { persona: string; providers: string[] } = { ...m.extra, persona: m.persona, providers: [...m.providers] };
  if (m.codeTools === 'mcp') out.code_tools = 'mcp';
  if (m.host) out.host = m.host;
  return out;
}

/** The keys of a team file that the editor does not model; merged back into the payload on save. */
export function teamExtras(d: TeamFileData): Record<string, unknown> {
  return unknownKeys(d, TEAM_KEYS);
}

/** Turns a team file (critic may be one member or a list, members may be plain ids) into the editor shape. */
export function normalizeTeam(d: TeamFileData): TeamData {
  const critics = Array.isArray(d.critic) ? d.critic : d.critic !== undefined ? [d.critic] : [];
  const out: TeamData = {
    id: d.id,
    name: d.name || d.id,
    expert: memberData(normMember(d.expert)),
    critic: critics.map((c) => memberData(normMember(c))),
    judge: memberData(normMember(d.judge)),
  };
  if (Array.isArray(d.proposers) && d.proposers.length) out.proposers = d.proposers.map((m) => memberData(normMember(m)));
  if (typeof d.max_rounds === 'number') out.max_rounds = d.max_rounds;
  if (typeof d.category === 'string' && d.category) out.category = d.category;
  if (Array.isArray(d.allowed_tools)) out.allowed_tools = d.allowed_tools.map(String);
  if (d.context && Array.isArray(d.context.paths) && d.context.paths.length) {
    out.context = { paths: d.context.paths.map(String) };
    if (d.context.ref) out.context.ref = String(d.context.ref);
  }
  return out;
}

// --- Tones -------------------------------------------------------------------------------------

export function jobStatusTone(status: string): Tone {
  switch (status) {
    case 'running':
      return 'info';
    case 'completed':
      return 'ok';
    case 'failed':
      return 'crit';
    default:
      return 'warn';
  }
}

/** Actions a human can decide on now (PROPOSED). HELD actions wait for the jev gate and are counted apart. */
export function isAwaitingDecision(a: ActionItem): boolean {
  return String(a.status).toUpperCase() === 'PROPOSED';
}

export function isHeld(a: ActionItem): boolean {
  return String(a.status).toUpperCase() === 'HELD';
}

/** Approvals status filter: everything, or one of the three groups the list is ordered by. */
export type ActionFilter = 'all' | 'awaiting' | 'held' | 'decided';

export function matchesActionFilter(a: ActionItem, filter: ActionFilter): boolean {
  switch (filter) {
    case 'awaiting':
      return isAwaitingDecision(a);
    case 'held':
      return isHeld(a);
    case 'decided':
      return !isAwaitingDecision(a) && !isHeld(a);
    default:
      return true;
  }
}

/** PROPOSED first, then HELD, then decided ones; the hub's order is kept within each group. */
export function sortActionsByDecision(actions: ActionItem[]): ActionItem[] {
  const rank = (a: ActionItem) => (isAwaitingDecision(a) ? 0 : isHeld(a) ? 1 : 2);
  return actions
    .map((a, i) => ({ a, i }))
    .sort((x, y) => rank(x.a) - rank(y.a) || x.i - y.i)
    .map((e) => e.a);
}

export function actionStatusTone(status: string): Tone {
  switch (String(status).toUpperCase()) {
    case 'PROPOSED':
    case 'RUNNING':
      return 'info';
    case 'HELD':
    case 'STALE':
      return 'warn';
    case 'EXECUTED':
      return 'ok';
    case 'FAILED':
    case 'INVALID':
      return 'crit';
    default:
      return 'idle'; // REJECTED and unknown
  }
}

export function riskTone(risk: string | undefined): Tone {
  const r = (risk || '').toLowerCase();
  if (r === 'high') return 'crit';
  if (r === 'medium') return 'warn';
  if (r === 'low') return 'ok';
  return 'idle';
}

export function riskLabel(risk: string | undefined): string {
  const r = (risk || '').toLowerCase();
  return tr.debate.lib.risk[r] ?? (risk ? tr.debate.lib.riskOther(risk.toUpperCase()) : tr.debate.lib.risk.unknown);
}

/** Only an explicit low or medium risk may be run with a checkbox; anything else needs the typed id. */
export function needsTypedConfirm(risk: string | undefined): boolean {
  const r = (risk || '').toLowerCase();
  return r !== 'low' && r !== 'medium';
}

export function roleTone(role: string): Tone {
  if ((role || '').includes('Expert')) return 'info';
  if ((role || '').includes('Critic')) return 'warn';
  return 'ok';
}

export function jevGateTone(gate: string): Tone {
  const g = gate.toUpperCase();
  if (g === 'PASSED') return 'ok';
  return 'warn';
}

// --- Debate result -----------------------------------------------------------------------------

/** Mirrors the Python decision_status: unverified, then the jev gate, then the approved flag. */
export function decisionStatus(fd: DebateFinalDecision | null | undefined): { label: string; tone: Tone } {
  if (!fd) return { label: 'NOT APPROVED', tone: 'warn' };
  if (fd.unverified) return { label: 'UNVERIFIED', tone: 'warn' };
  const gate = String(fd.jev_gate || '').toUpperCase();
  if (gate.includes('DISPUT')) return { label: 'DISPUTED', tone: 'warn' };
  if (gate.includes('PEND')) return { label: 'PENDING_JEV', tone: 'warn' };
  if (fd.approved) return { label: 'APPROVED', tone: 'ok' };
  return { label: 'NOT APPROVED', tone: 'warn' };
}

/** Korean display label of a decision label (`APPROVED`, `NOT APPROVED`, ...); the raw label stays the logic value. */
export function decisionText(label: string): string {
  return tr.debate.decisionLabel[label] ?? label;
}

/**
 * Decision status from the status word the engine reports (`debate_finished.status`, thread status):
 * APPROVED, UNVERIFIED, DISPUTED, PENDING_JEV and REVISION_REQUIRED. Older runs say IN_PROGRESS or
 * "NOT APPROVED" for the same not-approved outcome and are still accepted. Null for anything else
 * (job states such as completed or failed).
 */
export function decisionWordStatus(word: string | null | undefined): { label: string; tone: Tone } | null {
  switch (String(word || '').trim().toUpperCase().replace(/[\s-]+/g, '_')) {
    case 'APPROVED':
      return { label: 'APPROVED', tone: 'ok' };
    case 'UNVERIFIED':
      return { label: 'UNVERIFIED', tone: 'warn' };
    case 'DISPUTED':
      return { label: 'DISPUTED', tone: 'warn' };
    case 'PENDING_JEV':
      return { label: 'PENDING_JEV', tone: 'warn' };
    case 'REVISION_REQUIRED':
    case 'NOT_APPROVED':
    case 'IN_PROGRESS':
      return { label: 'NOT APPROVED', tone: 'warn' };
    default:
      return null;
  }
}

/**
 * How a finished run ended as a decision: the engine's status word first (`debate_finished.status`), else the
 * verdict and jev gate seen in the events. Null when the run reached no decision.
 */
export function runDecision(run: RunState): { label: string; tone: Tone } | null {
  const fromWord = decisionWordStatus(run.status);
  if (fromWord) return fromWord;
  if (!run.verdict) return null;
  return decisionStatus({ approved: run.verdict.approved, unverified: run.verdict.unverified, jev_gate: run.gate?.gate });
}

/** Debates whose thread is not closed, unless closed ones are asked for. */
export function visibleJobs<T extends { thread_closed?: boolean }>(jobs: T[], showClosed: boolean): T[] {
  return showClosed ? jobs : jobs.filter((j) => !j.thread_closed);
}

/** True when a result can be closed: not APPROVED and the thread is still open. */
export function canCloseThread(label: string, closed: boolean | undefined): boolean {
  return !closed && label !== 'APPROVED';
}

/** True when the gate is one the user can re-run (PENDING or DISPUTED). */
export function gateNeedsRecheck(fd: DebateFinalDecision | null | undefined): boolean {
  return ['PENDING', 'DISPUTED'].includes(String(fd?.jev_gate || '').toUpperCase());
}

export function teamLabel(t: DebateResult['team'] | string | undefined): string {
  if (!t) return tr.debate.lib.defaultTeam;
  if (typeof t === 'string') return t;
  return t.id || t.name || tr.debate.lib.defaultTeam;
}

export function groupByRound(items: DebateTranscriptItem[]): Array<[number, DebateTranscriptItem[]]> {
  const rounds = new Map<number, DebateTranscriptItem[]>();
  for (const item of items || []) {
    const r = Number(item.round) || 0;
    const list = rounds.get(r) ?? [];
    list.push(item);
    rounds.set(r, list);
  }
  return Array.from(rounds.entries()).sort((a, b) => a[0] - b[0]);
}

// --- Time and size -----------------------------------------------------------------------------

export function parseTime(s: string | null | undefined): number | null {
  if (!s) return null;
  const t = Date.parse(s);
  if (Number.isNaN(t) || new Date(t).getUTCFullYear() < 2000) return null;
  return t;
}

export function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(s / 60);
  return m ? tr.debate.lib.durationMS(m, s % 60) : tr.debate.lib.durationS(s);
}

/** Elapsed time of a job; a running job counts up to `now`. */
export function elapsedText(job: DebateJob, now: number): string {
  const start = parseTime(job.started_at);
  if (start === null) return '-';
  if (job.status === 'running') return fmtDuration(now - start);
  const end = parseTime(job.finished_at);
  return end === null ? '-' : fmtDuration(end - start);
}

export function fmtKB(bytes: number): string {
  return `${(Math.max(0, Number(bytes) || 0) / 1024).toFixed(1)} KB`;
}

export function fmtTime(s: string | undefined): string {
  if (!s) return '';
  const t = Date.parse(s.includes('T') ? s : s.replace(' ', 'T'));
  return Number.isNaN(t) ? s : fmtDateTime(t);
}

// --- Actions -----------------------------------------------------------------------------------

export function pretty(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2) ?? String(v);
  } catch {
    return String(v);
  }
}

export interface PatchEdit {
  file_path?: unknown;
  old?: unknown;
  new?: unknown;
}

export function patchEdits(a: ActionItem): PatchEdit[] {
  const e = (a.arguments as Record<string, unknown> | null | undefined)?.edits;
  return Array.isArray(e) ? (e as PatchEdit[]) : [];
}

/** "3 edits in 2 files" */
export function patchSummary(a: ActionItem): string {
  const edits = patchEdits(a);
  const files = new Set(edits.map((e) => String(e?.file_path ?? '')));
  return tr.debate.lib.patchSummary(edits.length, files.size);
}

/** Tools whose run changes files in the hub workspace and is refused when the workspace moved. */
export function changesWorkspace(tool: string): boolean {
  return tool === 'jev_mcp:execute_code_action' || tool === 'jev_mcp:apply_patch';
}

export type DiffLineKind = 'header' | 'hunk' | 'add' | 'del' | 'ctx';

export function diffLineKind(line: string): DiffLineKind {
  if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) return 'header';
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return 'ctx';
}

export type FailKind = 'execute' | 'publish' | 'recheck' | 'close' | 'other';

/**
 * Message for a failed hub action. Only `execute` gets the run guidance (`ref` is the action id) and only `publish`
 * the publish CLI hint (`ref` is the thread id); recheck, close and other errors show the hub's own text, which
 * already says why (for example that a newer round exists).
 */
export function failMessage(e: unknown, kind: FailKind = 'other', ref?: number | string): string {
  const text = errorText(e);
  if (e instanceof HubError) {
    if (e.status === 400) return tr.debate.lib.invalidRequest(text);
    if (e.status === 404) return tr.debate.lib.notFound(text);
    if (kind === 'execute') {
      if (e.status === 403 && !/guardian\.py/.test(text))
        return `${text}\n\n${tr.debate.lib.uiActionsOff(`guardian.py --run-action ${ref ?? 'ID'}`)}`;
      if (e.status === 409 && /workspace moved/i.test(text))
        return `${text}\n\n${tr.debate.lib.workspaceMoved}`;
      if (e.status === 409)
        return `${text}\n\n${tr.debate.lib.notRunnable}`;
    }
    if (kind === 'publish' && e.status === 403 && !/guardian\.py/.test(text)) return `${text}\n\n${tr.debate.lib.publishViaCli(`guardian.py --publish-thread ${ref ?? 'ID'}`)}`;
  }
  return text;
}

// --- Publish -----------------------------------------------------------------------------------

/** Branch names the hub accepts for a publish (mirrors debate_handler.go). */
export const BRANCH_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$/;

/** Tools whose executed run writes files into the hub workspace (the ones a publish commits). */
export function isFileAction(tool: string): boolean {
  const name = String(tool).split(':').pop();
  return name === 'execute_code_action' || name === 'apply_patch';
}

/** An action nobody has to decide on or wait for any more (not PROPOSED, HELD or RUNNING). */
export function isSettledAction(a: ActionItem): boolean {
  return !['PROPOSED', 'HELD', 'RUNNING'].includes(String(a.status).toUpperCase());
}

export type PlanState = 'running' | 'awaiting' | 'held' | 'failed' | 'done' | 'rejected';

/** One word for where a plan stands, from its step counts. */
export function planState(plan: Pick<PlanItem, 'counts'>): PlanState {
  const n = (s: string) => plan.counts[s] ?? 0;
  if (n('RUNNING') > 0) return 'running';
  if (n('PROPOSED') > 0) return 'awaiting';
  if (n('HELD') > 0) return 'held';
  if (n('FAILED') + n('STALE') + n('INVALID') + n('SKIPPED') > 0) return 'failed';
  if (n('EXECUTED') > 0) return 'done';
  return 'rejected';
}

export function planStateTone(state: PlanState): Tone {
  return { running: 'info', awaiting: 'info', held: 'warn', failed: 'crit', done: 'ok', rejected: 'idle' }[state] as Tone;
}

/** `#/approvals/:param` is a plan id (otherwise an action id). */
export const PLAN_ID_RE = /^plan_[A-Za-z0-9_]{1,100}$/;

/** Whether a thread can be published now, from its live action list; `reason` says what is missing. */
export function publishReadiness(actions: ActionItem[]): { ready: boolean; reason: string } {
  const open = actions.filter((a) => !isSettledAction(a)).length;
  if (open > 0) return { ready: false, reason: tr.debate.lib.stillOpen(open) };
  const files = actions.filter((a) => isFileAction(a.tool) && String(a.status).toUpperCase() === 'EXECUTED');
  if (files.length === 0) return { ready: false, reason: tr.debate.lib.noFileChange };
  return { ready: true, reason: '' };
}

/**
 * Markdown for the judge's decision. Older results stored it on one line (the engine collapsed the judge's
 * line breaks): when such a line enumerates ` [1] … [2] …` in order, each item starts its own paragraph.
 * Text that already has line breaks is returned unchanged.
 */
export function structureDecision(text: string): string {
  const t = text.trim();
  if (t.includes('\n')) return t;
  const cuts: number[] = [];
  let next = 1;
  for (const m of t.matchAll(/(^|\s)\[(\d{1,2})\](?=\s)/g)) {
    if (Number(m[2]) !== next) continue;
    cuts.push((m.index ?? 0) + m[1].length);
    next += 1;
  }
  if (cuts.length < 2) return t;
  const parts: string[] = [];
  let from = 0;
  for (const at of cuts) {
    parts.push(t.slice(from, at).trim());
    from = at;
  }
  parts.push(t.slice(from).trim());
  return parts
    .filter(Boolean)
    .map((p) => p.replace(/^\[(\d{1,2})\]\s*/, '**[$1]** '))
    .join('\n\n');
}

// --- Structured view of the judge's decision ------------------------------------------------------

/** Requirement ids the new judges use (`[R1]`); older results number them `[1]`. */
export const REQ_ID_RE = /^R\d{1,2}$/;

export interface DecisionRequirement {
  /** `R1` for new judges, `1` for legacy numbering. */
  id: string;
  title: string;
  text: string;
}

export interface DecisionExtra {
  title: string;
  text: string;
}

export interface ParsedDecision {
  intro: string;
  requirements: DecisionRequirement[];
  extras: DecisionExtra[];
  /** Paragraphs after the last item (may be empty). */
  conclusion: string;
}

const ITEM_MARKER_RE = /^\s*(?:\*\*)?\[(R\d{1,2}|\d{1,2})\](?:\*\*)?[ \t]*/;
const EXTRA_MARKER_RE = /^\s*(?:\*\*)?\[(추가|Extra)\](?:\*\*)?[ \t]*/i;
const TITLE_MAX = 40;

/** Text before the first ":" as a title when it is short enough; everything else is body. */
export function splitTitle(rest: string): { title: string; text: string } {
  const body = rest.trim();
  const m = /[:：]/.exec(body);
  if (m && m.index > 0 && m.index <= TITLE_MAX) {
    const title = body.slice(0, m.index).replace(/^\*+|\*+$/g, '').trim();
    if (title) return { title, text: body.slice(m.index + 1).trim() };
  }
  return { title: '', text: body };
}

/**
 * Splits a decision into summary, requirement items (`[R1] title: text`, legacy `[1] ...`), extras (`[추가]`,
 * `[Extra]`) and the closing paragraphs. Returns null when the text carries no item marker at all.
 */
export function parseDecision(text: string): ParsedDecision | null {
  let t = structureDecision(text);
  // A one-line legacy decision: extras also start their own paragraph.
  if (!text.trim().includes('\n')) t = t.replace(/\s+(?=(?:\*\*)?\[(?:추가|Extra)\](?:\*\*)?\s)/gi, '\n\n');
  // Paragraphs: a blank line ends one, a line that starts with a marker begins a new one.
  const paras: string[] = [];
  let cur: string[] = [];
  const flush = () => {
    if (cur.length) paras.push(cur.join('\n'));
    cur = [];
  };
  for (const line of t.split('\n')) {
    if (line.trim() === '') flush();
    else {
      if (ITEM_MARKER_RE.test(line) || EXTRA_MARKER_RE.test(line)) flush();
      cur.push(line);
    }
  }
  flush();

  const intro: string[] = [];
  const requirements: DecisionRequirement[] = [];
  const extras: DecisionExtra[] = [];
  let conclusion: string[] = [];
  const raw: Array<{ kind: 'req' | 'extra'; id: string; first: string }> = [];
  // Plain paragraphs between two items belong to the earlier item; only those after the last stay the conclusion.
  const absorb = () => {
    if (raw.length && conclusion.length) raw[raw.length - 1].first += `\n\n${conclusion.join('\n\n')}`;
    conclusion = [];
  };
  for (const p of paras) {
    const im = ITEM_MARKER_RE.exec(p);
    const em = im ? null : EXTRA_MARKER_RE.exec(p);
    if (im || em) absorb();
    if (im) {
      raw.push({ kind: 'req', id: im[1], first: p.slice(im[0].length) });
    } else if (em) {
      raw.push({ kind: 'extra', id: '', first: p.slice(em[0].length) });
    } else if (raw.length === 0) intro.push(p);
    else conclusion.push(p);
  }
  if (raw.length === 0) return null;
  for (const r of raw) {
    const { title, text: body } = splitTitle(r.first);
    if (r.kind === 'req') requirements.push({ id: r.id, title, text: body });
    else extras.push({ title, text: body });
  }
  return { intro: intro.join('\n\n'), requirements, extras, conclusion: conclusion.join('\n\n') };
}

// --- Rationale -----------------------------------------------------------------------------------

export interface RationaleAlternative {
  label: string;
  text: string;
}

export interface RationaleItem {
  /** The item number, or '' for text without numbering. */
  num: string;
  title: string;
  /** Body before the alternatives (the whole body when there are none). */
  text: string;
  alternatives: RationaleAlternative[];
}

/** `(a) ... (b) ... (c) ...` in order from (a); fewer than two alternatives are left in the text. */
function splitAlternatives(text: string): { lead: string; alternatives: RationaleAlternative[] } {
  const cuts: Array<{ at: number; end: number; label: string }> = [];
  let want = 'a'.charCodeAt(0);
  for (const m of text.matchAll(/(^|\s)\(([a-z])\)\s*/g)) {
    if (m[2].charCodeAt(0) !== want) continue;
    cuts.push({ at: (m.index ?? 0) + m[1].length, end: (m.index ?? 0) + m[0].length, label: m[2] });
    want += 1;
  }
  if (cuts.length < 2) return { lead: text, alternatives: [] };
  const alternatives = cuts.map((c, i) => ({
    label: c.label,
    text: text.slice(c.end, i + 1 < cuts.length ? cuts[i + 1].at : undefined).trim().replace(/[;,]\s*$/, ''),
  }));
  return { lead: text.slice(0, cuts[0].at).trim(), alternatives };
}

/** Splits `1) title: text` / `1. title: text` lines into blocks; text without numbering becomes one block. */
export function splitRationale(text: string): RationaleItem[] {
  const groups: Array<{ num: string; lines: string[] }> = [];
  for (const line of text.split('\n')) {
    const m = /^\s*(\d{1,2})[).]\s+(.*)$/.exec(line);
    if (m) groups.push({ num: m[1], lines: [m[2]] });
    else if (groups.length) groups[groups.length - 1].lines.push(line);
    else if (line.trim()) groups.push({ num: '', lines: [line] });
  }
  return groups.map((g) => {
    const { title, text: body } = splitTitle(g.lines.join('\n').trim());
    const { lead, alternatives } = splitAlternatives(body);
    return { num: g.num, title, text: lead, alternatives };
  });
}

// --- Files named in the decision ------------------------------------------------------------------

const FILE_TOKEN_RE = /[\w./-]+\.(?:launch\.py|py|ya?ml|md|json|sh|tsx|ts|go|xml|toml)(?![\w])/g;

export function basename(path: string): string {
  return path.split('/').pop() || path;
}

/** File names mentioned in the texts, once per basename, in order of appearance. */
export function mentionedFiles(texts: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const t of texts) {
    for (const m of t.matchAll(FILE_TOKEN_RE)) {
      const token = m[0].replace(/^(\.\/|[-.]+)/, '');
      const base = basename(token);
      if (!token || !base || seen.has(base)) continue;
      seen.add(base);
      out.push(token);
    }
  }
  return out;
}

/** Mentioned files that no change writes (compared by basename). */
export function unwrittenMentions(mentioned: string[], writtenPaths: string[]): string[] {
  const written = new Set(writtenPaths.map(basename));
  return mentioned.filter((f) => !written.has(basename(f)));
}

export interface FileChange {
  /** Action id. */
  id: number | string;
  path: string;
  status: string;
  reason: string;
  /** The action declares run_test: false. */
  noTest: boolean;
  content: string | null;
  bytes: number;
  refs: string[];
}

function dirOf(path: string): string {
  const i = path.lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
}

export { dirOf as dirname };

/** Files the thread's actions write: from the live list when there is one, else from the result snapshot; sorted by directory. */
export function buildFileChanges(live: ActionItem[] | null, snapshot: DebateAction[]): FileChange[] {
  const out: FileChange[] = [];
  const enc = new TextEncoder();
  if (live) {
    for (const a of live) {
      const args = a.arguments ?? {};
      const path = typeof args.file_path === 'string' ? args.file_path : '';
      if (!path) continue;
      const snap = snapshot.find((x) => String(x.id) === String(a.id));
      const content = typeof args.content === 'string' ? args.content : null;
      out.push({
        id: a.id,
        path,
        status: String(a.status),
        reason: a.reason ?? '',
        noTest: args.run_test === false,
        content,
        bytes: content === null ? 0 : enc.encode(content).length,
        refs: snap?.refs ?? [],
      });
    }
  } else {
    for (const s of snapshot) {
      if (!s.file_path) continue;
      out.push({ id: s.id, path: s.file_path, status: String(s.status), reason: '', noTest: false, content: null, bytes: 0, refs: s.refs ?? [] });
    }
  }
  return out
    .map((c, i) => ({ c, i }))
    .sort((x, y) => dirOf(x.c.path).localeCompare(dirOf(y.c.path)) || basename(x.c.path).localeCompare(basename(y.c.path)) || x.i - y.i)
    .map((e) => e.c);
}

// --- Traceability ---------------------------------------------------------------------------------

export interface RequirementLinks {
  constraints: number[];
  checks: number[];
  files: string[];
}

export function requirementLinks(id: string, fd: DebateFinalDecision, files: FileChange[]): RequirementLinks {
  const idx = (refs: string[][] | undefined) =>
    (refs ?? []).flatMap((r, i) => (Array.isArray(r) && r.includes(id) ? [i] : []));
  return {
    constraints: idx(fd.constraint_refs),
    checks: idx(fd.check_refs),
    files: [...new Set(files.filter((f) => f.refs.includes(id)).map((f) => f.path))],
  };
}

/** Whether the result carries any requirement link at all (legacy results do not). */
export function hasTraceRefs(fd: DebateFinalDecision): boolean {
  const some = (r: string[][] | undefined) => (r ?? []).some((x) => Array.isArray(x) && x.length > 0);
  return some(fd.constraint_refs) || some(fd.check_refs) || (fd.actions ?? []).some((a) => (a.refs ?? []).length > 0);
}

// --- Raw JSON inside the judgment -----------------------------------------------------------------

export interface JudgmentSplit {
  markdown: string;
  blocks: Array<{ kind: 'decision' | 'findings'; text: string }>;
}

/**
 * Takes the judge's machine-readable fences out of its Markdown: a ```json fence holding an object with a
 * "decision" key, and any ```findings fence. A fence that does not parse stays where it is.
 */
export function extractJudgmentJson(markdown: string): JudgmentSplit {
  const blocks: JudgmentSplit['blocks'] = [];
  const stripped = markdown.replace(/^```([^\n`]*)\n([\s\S]*?)\n```[ \t]*$/gm, (whole, info: string, body: string) => {
    const lang = info.trim().toLowerCase();
    if (lang === 'json') {
      try {
        const v: unknown = JSON.parse(body);
        if (v && typeof v === 'object' && !Array.isArray(v) && 'decision' in v) {
          blocks.push({ kind: 'decision', text: JSON.stringify(v, null, 2) });
          return '';
        }
      } catch {
        /* not JSON: leave the fence */
      }
      return whole;
    }
    if (lang === 'findings') {
      let text = body;
      try {
        text = JSON.stringify(JSON.parse(body), null, 2);
      } catch {
        /* plain text findings */
      }
      blocks.push({ kind: 'findings', text });
      return '';
    }
    return whole;
  });
  return { markdown: stripped.replace(/\n{3,}/g, '\n\n').trim(), blocks };
}


// --- Team list: categories, sorting, grouping -------------------------------------------------

/** Group order of the team list; teams without a (known) category go last as "other". */
export const TEAM_CATEGORY_ORDER = ['development', 'operations', 'design-docs', 'general'] as const;
export const TEAM_CATEGORY_OTHER = 'other';
export type TeamSort = 'created' | 'modified' | 'name';

export function teamCategory(team: TeamEntry): string {
  const c = team.data?.category;
  return c && (TEAM_CATEGORY_ORDER as readonly string[]).includes(c) ? c : TEAM_CATEGORY_OTHER;
}

function stamp(value: string | undefined): number {
  const t = value ? Date.parse(value) : NaN;
  return Number.isNaN(t) ? 0 : t;
}

export function teamCreated(team: TeamEntry): number {
  return stamp(team.data?.created);
}

/** Never mutates its input. Dates sort newest first (undated teams last); ties and names fall back to the id. */
export function sortTeams(teams: TeamEntry[], by: TeamSort): TeamEntry[] {
  const key = (t: TeamEntry): number => (by === 'modified' ? stamp(t.data?.modified) || teamCreated(t) : teamCreated(t));
  const byName = (a: TeamEntry, b: TeamEntry) => (a.name || a.id).localeCompare(b.name || b.id) || a.id.localeCompare(b.id);
  return [...teams].sort((a, b) => (by === 'name' ? byName(a, b) : key(b) - key(a) || a.id.localeCompare(b.id)));
}

/** Non-empty groups in category order; each group keeps the order of `teams` (sort first). */
export function groupTeams(teams: TeamEntry[]): Array<{ category: string; teams: TeamEntry[] }> {
  return [...TEAM_CATEGORY_ORDER, TEAM_CATEGORY_OTHER]
    .map((category) => ({ category, teams: teams.filter((t) => teamCategory(t) === category) }))
    .filter((g) => g.teams.length > 0);
}

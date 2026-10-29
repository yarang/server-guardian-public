import { describe, expect, it } from 'vitest';
import { HubError } from '../src/api';
import {
  structureDecision,
  parseDecision,
  splitRationale,
  mentionedFiles,
  unwrittenMentions,
  extractJudgmentJson,
  buildFileChanges,
  requirementLinks,
  hasTraceRefs,
  canCloseThread,
  BRANCH_RE,
  checkGlobs,
  decisionStatus,
  decisionWordStatus,
  diffLineKind,
  elapsedText,
  failMessage,
  gateNeedsRecheck,
  groupTeams,
  groupByRound,
  needsTypedConfirm,
  normMember,
  memberData,
  normalizeTeam,
  teamExtras,
  parseGlobLines,
  patchSummary,
  publishReadiness,
  sortTeams,
  teamCategory,
  visibleJobs,
} from '../src/lib/debate';
import { ko } from '../src/i18n/ko';
import { action } from './debateFixtures';
import type { TeamEntry } from '../src/types';

describe('decisionStatus (mirrors the Python decision_status)', () => {
  it('orders unverified, then the jev gate, then approved', () => {
    expect(decisionStatus(null).label).toBe('NOT APPROVED');
    expect(decisionStatus({ unverified: true, approved: true, jev_gate: 'PASSED' }).label).toBe('UNVERIFIED');
    expect(decisionStatus({ jev_gate: 'DISPUTED', approved: true }).label).toBe('DISPUTED');
    expect(decisionStatus({ jev_gate: 'PENDING' }).label).toBe('PENDING_JEV');
    expect(decisionStatus({ approved: true, jev_gate: 'PASSED' })).toEqual({ label: 'APPROVED', tone: 'ok' });
    expect(decisionStatus({ approved: false }).label).toBe('NOT APPROVED');
  });

  it('offers the jev re-check only for PENDING and DISPUTED', () => {
    expect(gateNeedsRecheck({ jev_gate: 'pending' })).toBe(true);
    expect(gateNeedsRecheck({ jev_gate: 'DISPUTED' })).toBe(true);
    expect(gateNeedsRecheck({ jev_gate: 'PASSED' })).toBe(false);
    expect(gateNeedsRecheck(null)).toBe(false);
  });
});

describe('reference code checks', () => {
  it('parses one trimmed glob per line and skips blanks', () => {
    expect(parseGlobLines(' a/*.go \n\n  b.py:1-5\n')).toEqual(['a/*.go', 'b.py:1-5']);
  });

  it('rejects too many paths, long paths and a leading dash', () => {
    expect(checkGlobs(['a/*.go', 'b#Func', 'c.go:10-20'])).toBe('');
    expect(checkGlobs(Array.from({ length: 21 }, (_, i) => `f${i}`))).toContain(ko.debate.lib.globsTooMany(20, 21));
    expect(checkGlobs(['x'.repeat(201)])).toContain(ko.debate.lib.globTooLong(200));
    expect(checkGlobs(['-rf'])).toContain(ko.debate.lib.globDash('-rf'));
  });
});

describe('diff and action helpers', () => {
  it('classifies diff lines', () => {
    expect(diffLineKind('--- a/x')).toBe('header');
    expect(diffLineKind('+++ b/x')).toBe('header');
    expect(diffLineKind('diff --git a b')).toBe('header');
    expect(diffLineKind('@@ -1 +1 @@')).toBe('hunk');
    expect(diffLineKind('+new')).toBe('add');
    expect(diffLineKind('-old')).toBe('del');
    expect(diffLineKind(' ctx')).toBe('ctx');
  });

  it('asks for the typed id unless the risk is explicitly low or medium', () => {
    expect(needsTypedConfirm('low')).toBe(false);
    expect(needsTypedConfirm('medium')).toBe(false);
    expect(needsTypedConfirm('high')).toBe(true);
    expect(needsTypedConfirm(undefined)).toBe(true);
    expect(needsTypedConfirm('weird')).toBe(true);
  });

  it('summarizes patches', () => {
    const a = action({ tool: 'jev_mcp:apply_patch', arguments: { edits: [{ file_path: 'a.go' }, { file_path: 'a.go' }, { file_path: 'b.go' }] } });
    expect(patchSummary(a)).toBe(ko.debate.lib.patchSummary(3, 2));
    expect(patchSummary(action({ arguments: { edits: [{ file_path: 'a.go' }] } }))).toBe(ko.debate.lib.patchSummary(1, 1));
  });

  it('adds guidance to the hub status codes', () => {
    expect(failMessage(new HubError('nope', 403), 'execute')).toContain(ko.debate.lib.uiActionsOff('guardian.py --run-action ID'));
    expect(failMessage(new HubError('workspace moved from a to b', 409), 'execute')).toContain(ko.debate.lib.workspaceMoved);
    expect(failMessage(new HubError('busy', 409), 'execute')).toContain(ko.debate.lib.notRunnable);
    expect(failMessage(new HubError('bad id', 400))).toBe(ko.debate.lib.invalidRequest('bad id'));
    expect(failMessage(new HubError('gone', 404))).toBe(ko.debate.lib.notFound('gone'));
    expect(failMessage(new Error('plain'))).toBe('plain');
  });

  it('keeps the Execute hints out of the other kinds', () => {
    const newer = 'a later round exists; debate again instead of rechecking the old decision';
    for (const kind of ['recheck', 'close', 'other'] as const) {
      expect(failMessage(new HubError(newer, 409), kind)).toBe(newer);
      expect(failMessage(new HubError('off', 403), kind)).toBe('off');
    }
    expect(failMessage(new HubError('x', 409), 'publish')).toBe('x');
    expect(failMessage(new HubError('bad', 400), 'recheck')).toBe(ko.debate.lib.invalidRequest('bad'));
  });

  it('gives only publish its CLI hint', () => {
    expect(failMessage(new HubError('off', 403), 'publish', 'th_1')).toContain('guardian.py --publish-thread th_1');
    expect(failMessage(new HubError('off', 403), 'publish', 'th_1')).not.toContain('--run-action');
  });
});

describe('team and transcript helpers', () => {
  it('normalizes plain-id members and a single critic', () => {
    const t = normalizeTeam({ id: 'x', expert: 'e', critic: 'c', judge: { persona: 'j', providers: ['claude'] }, allowed_tools: [], context: { paths: ['a'], ref: 'main' } });
    expect(t.name).toBe('x');
    expect(t.expert).toEqual({ persona: 'e', providers: [] });
    expect(t.critic).toEqual([{ persona: 'c', providers: [] }]);
    expect(t.judge.providers).toEqual(['claude']);
    expect(t.allowed_tools).toEqual([]);
    expect(t.context).toEqual({ paths: ['a'], ref: 'main' });
  });

  it('keeps code_tools mcp on members and drops other values', () => {
    const t = normalizeTeam({
      id: 'x',
      expert: { persona: 'e', code_tools: 'mcp' },
      critic: [{ persona: 'c1', code_tools: null }, { persona: 'c2', providers: ['claude'], code_tools: 'mcp' }, 'c3'],
      judge: { persona: 'j', code_tools: 'bogus' as never },
    });
    expect(t.expert).toEqual({ persona: 'e', providers: [], code_tools: 'mcp' });
    expect(t.critic[0]).toEqual({ persona: 'c1', providers: [] });
    expect(t.critic[1]).toEqual({ persona: 'c2', providers: ['claude'], code_tools: 'mcp' });
    expect(t.critic[2]).toEqual({ persona: 'c3', providers: [] });
    expect(t.judge).toEqual({ persona: 'j', providers: [] });
  });

  it('groups the transcript by ascending round', () => {
    const grouped = groupByRound([
      { round: 2, agent: 'a', role: 'r', content: '' },
      { round: 1, agent: 'b', role: 'r', content: '' },
      { round: 1, agent: 'c', role: 'r', content: '' },
    ]);
    expect(grouped.map(([r, items]) => [r, items.length])).toEqual([[1, 2], [2, 1]]);
  });

  it('computes elapsed time for running and finished jobs', () => {
    const base = { id: 'j', task: '', team: '', thread_id: '', rounds: 0, exit_code: null, error: '' };
    const start = Date.parse('2026-10-04T10:00:00Z');
    expect(elapsedText({ ...base, status: 'running', started_at: '2026-10-04T10:00:00Z', finished_at: '' }, start + 65_000)).toBe(ko.debate.lib.durationMS(1, 5));
    expect(elapsedText({ ...base, status: 'completed', started_at: '2026-10-04T10:00:00Z', finished_at: '2026-10-04T10:00:09Z' }, start + 99_000)).toBe(ko.debate.lib.durationS(9));
    expect(elapsedText({ ...base, status: 'failed', started_at: '', finished_at: '' }, start)).toBe('-');
  });
});

describe('closed threads', () => {
  it('visibleJobs hides jobs of closed threads unless asked', () => {
    const jobs = [{ id: 'a' }, { id: 'b', thread_closed: true }, { id: 'c', thread_closed: false }];
    expect(visibleJobs(jobs, false).map((j) => j.id)).toEqual(['a', 'c']);
    expect(visibleJobs(jobs, true)).toHaveLength(3);
  });

  it('canCloseThread is false for APPROVED and for closed threads', () => {
    expect(canCloseThread('DISPUTED', false)).toBe(true);
    expect(canCloseThread('NOT APPROVED', undefined)).toBe(true);
    expect(canCloseThread('APPROVED', false)).toBe(false);
    expect(canCloseThread('DISPUTED', true)).toBe(false);
  });
});

describe('publish helpers', () => {
  const a = (tool: string, status: string, id = 1) => action({ id, tool, status });

  it('is ready only with every action settled and an executed file action', () => {
    expect(publishReadiness([a('jev_mcp:apply_patch', 'EXECUTED')]).ready).toBe(true);
    expect(publishReadiness([a('jev_mcp:execute_code_action', 'EXECUTED'), a('run_tests', 'FAILED', 2), a('x', 'REJECTED', 3)]).ready).toBe(true);
    expect(publishReadiness([a('jev_mcp:apply_patch', 'EXECUTED'), a('run_tests', 'PROPOSED', 2)]).reason).toContain(ko.debate.lib.stillOpen(1));
    expect(publishReadiness([a('jev_mcp:apply_patch', 'EXECUTED'), a('x', 'HELD', 2), a('y', 'RUNNING', 3)]).reason).toContain(ko.debate.lib.stillOpen(2));
    expect(publishReadiness([a('run_tests', 'EXECUTED')]).reason).toContain(ko.debate.lib.noFileChange);
    expect(publishReadiness([a('jev_mcp:apply_patch', 'FAILED')]).ready).toBe(false);
    expect(publishReadiness([]).ready).toBe(false);
  });

  it('accepts the branch names the hub accepts', () => {
    for (const ok of ['guardian/thread_1', 'a', 'feature/x.y-z_1', 'A'.repeat(100)]) expect(BRANCH_RE.test(ok)).toBe(true);
    for (const bad of ['', '-x', '/x', '.x', 'a b', 'a'.repeat(101), 'a;b']) expect(BRANCH_RE.test(bad)).toBe(false);
  });

  it('names the action in the CLI hint of a 403', () => {
    expect(failMessage(new HubError('off', 403), 'execute', 57)).toContain('guardian.py --run-action 57');
  });
});

describe('decisionWordStatus (engine status words)', () => {
  it('maps the current and the old words for a debate that was not approved', () => {
    for (const w of ['REVISION_REQUIRED', 'IN_PROGRESS', 'NOT APPROVED', 'not_approved', 'revision-required']) {
      expect(decisionWordStatus(w)).toEqual({ label: 'NOT APPROVED', tone: 'warn' });
    }
  });

  it('maps the other decisions and ignores job states', () => {
    expect(decisionWordStatus('APPROVED')).toEqual({ label: 'APPROVED', tone: 'ok' });
    expect(decisionWordStatus('DISPUTED')?.tone).toBe('warn');
    expect(decisionWordStatus('PENDING_JEV')?.label).toBe('PENDING_JEV');
    expect(decisionWordStatus('UNVERIFIED')?.tone).toBe('warn');
    for (const w of ['completed', 'failed', 'running', '', null, undefined]) expect(decisionWordStatus(w)).toBeNull();
  });
});

describe('structureDecision', () => {
  it('splits an in-order [n] enumeration of a one-line decision into paragraphs', () => {
    expect(structureDecision('Approve with changes. [1] Security: safe_load. [2] Clock: sim time. [3] Done.')).toBe(
      'Approve with changes.\n\n**[1]** Security: safe_load.\n\n**[2]** Clock: sim time.\n\n**[3]** Done.'
    );
  });

  it('leaves text with line breaks, a single marker or out-of-order markers alone', () => {
    expect(structureDecision('Line one\n\n[1] a [2] b')).toBe('Line one\n\n[1] a [2] b');
    expect(structureDecision('Only [1] marker here')).toBe('Only [1] marker here');
    expect(structureDecision('see [2] and [1] refs')).toBe('see [2] and [1] refs');
    expect(structureDecision('arr[1] and arr[2] stay')).toBe('arr[1] and arr[2] stay');
  });
});

describe('parseDecision', () => {
  const full = [
    '전체 요약 문장입니다.',
    '[R1] 보안: safe_load를 쓴다.',
    '[R2] 시간 동기화: 시뮬레이션 시간을 쓴다.',
    '[추가] 로그: 로테이션을 건다.',
    '[Extra] Docs: update README.',
    '구현 첫 산출물은 MVP예요.',
  ].join('\n\n');

  it('splits intro, R-id requirements, extras and the conclusion', () => {
    const p = parseDecision(full)!;
    expect(p.intro).toBe('전체 요약 문장입니다.');
    expect(p.requirements.map((r) => [r.id, r.title, r.text])).toEqual([
      ['R1', '보안', 'safe_load를 쓴다.'],
      ['R2', '시간 동기화', '시뮬레이션 시간을 쓴다.'],
    ]);
    expect(p.extras).toEqual([
      { title: '로그', text: '로테이션을 건다.' },
      { title: 'Docs', text: 'update README.' },
    ]);
    expect(p.conclusion).toBe('구현 첫 산출물은 MVP예요.');
  });

  it('reads legacy [n] markers, including a one-line decision', () => {
    const p = parseDecision('Approve. [1] Security: a. [2] Clock: b. [추가] Misc: c.')!;
    expect(p.intro).toBe('Approve.');
    expect(p.requirements.map((r) => r.id)).toEqual(['1', '2']);
    expect(p.extras).toHaveLength(1);
    expect(p.conclusion).toBe('');
  });

  it('keeps plain paragraphs between two items with the earlier item', () => {
    const p = parseDecision('[R1] A: one\n\nmore of one\n\n[R2] B: two')!;
    expect(p.requirements[0].text).toBe('one\n\nmore of one');
    expect(p.conclusion).toBe('');
  });

  it('returns null without any marker', () => {
    expect(parseDecision('Just a sentence. arr[1] is not a marker.')).toBeNull();
  });

  it('uses a title only when the part before ":" is at most 40 characters', () => {
    const long = 'x'.repeat(41);
    const ok = 'y'.repeat(40);
    const p = parseDecision(`[R1] ${long}: body\n\n[R2] ${ok}: body\n\n[R3] no colon here`)!;
    expect(p.requirements[0]).toMatchObject({ title: '', text: `${long}: body` });
    expect(p.requirements[1]).toMatchObject({ title: ok, text: 'body' });
    expect(p.requirements[2]).toMatchObject({ title: '', text: 'no colon here' });
  });
});

describe('splitRationale', () => {
  it('splits numbered items and their (a)(b)(c) alternatives', () => {
    const items = splitRationale('1) 문제의 존재: 기존 코드는 위험하다.\n2) 대안 검토: 선택지는 (a) 그대로 둔다 (b) 패치한다 (c) 새로 쓴다\n3. 트레이드오프: 속도를 잃는다.');
    expect(items.map((i) => [i.num, i.title])).toEqual([['1', '문제의 존재'], ['2', '대안 검토'], ['3', '트레이드오프']]);
    expect(items[0]).toMatchObject({ text: '기존 코드는 위험하다.', alternatives: [] });
    expect(items[1].text).toBe('선택지는');
    expect(items[1].alternatives).toEqual([
      { label: 'a', text: '그대로 둔다' },
      { label: 'b', text: '패치한다' },
      { label: 'c', text: '새로 쓴다' },
    ]);
  });

  it('joins continuation lines and keeps un-numbered text as one block', () => {
    expect(splitRationale('1) A: first\nsecond line')[0].text).toBe('first\nsecond line');
    expect(splitRationale('just prose')).toEqual([{ num: '', title: '', text: 'just prose', alternatives: [] }]);
    expect(splitRationale('1) A: only (a) one alt')[0].alternatives).toEqual([]);
  });
});

describe('mentioned files', () => {
  it('finds file names once per basename and ignores plain words', () => {
    const found = mentionedFiles(['Edit `app/main.py` and config.yaml, then run.sh', 'also launch/sim.launch.py and main.py again', 'no files here, e.g. this.']);
    expect(found).toEqual(['app/main.py', 'config.yaml', 'run.sh', 'launch/sim.launch.py']);
  });

  it('lists the ones no change writes, by basename', () => {
    expect(unwrittenMentions(['app/main.py', 'README.md'], ['other/main.py'])).toEqual(['README.md']);
  });
});

describe('extractJudgmentJson', () => {
  const fence = (lang: string, body: string) => '```' + lang + '\n' + body + '\n```';

  it('moves a decision JSON fence and a findings fence out of the Markdown', () => {
    const md = `Reasoning.\n\n${fence('json', '{"decision":"x","a":1}')}\n\n${fence('findings', '[{"id":1}]')}\n\nTail.`;
    const r = extractJudgmentJson(md);
    expect(r.markdown).toBe('Reasoning.\n\nTail.');
    expect(r.blocks.map((b) => b.kind)).toEqual(['decision', 'findings']);
    expect(r.blocks[0].text).toBe(JSON.stringify({ decision: 'x', a: 1 }, null, 2));
  });

  it('leaves invalid JSON, JSON without "decision" and other fences in place', () => {
    for (const body of [fence('json', '{"decision": oops}'), fence('json', '{"a": 1}'), fence('json', '[1]'), fence('python', 'print(1)')]) {
      const r = extractJudgmentJson(`Intro\n\n${body}`);
      expect(r.markdown).toContain(body);
      expect(r.blocks).toEqual([]);
    }
  });
});

describe('file changes and traceability', () => {
  it('builds sorted file rows from live actions and merges refs from the snapshot', () => {
    const live = [
      action({ id: 2, tool: 'jev_mcp:execute_code_action', arguments: { file_path: 'b/z.py', content: 'abc', run_test: false }, status: 'EXECUTED', reason: 'why' }),
      action({ id: 1, tool: 'jev_mcp:execute_code_action', arguments: { file_path: 'a/y.py', content: 'é' }, status: 'PROPOSED' }),
      action({ id: 3, tool: 'run_tests', arguments: { command: 'go test' } }),
    ];
    const rows = buildFileChanges(live, [{ id: 2, tool: 't', status: 'X', refs: ['R1'] }]);
    expect(rows.map((r) => r.path)).toEqual(['a/y.py', 'b/z.py']);
    expect(rows[0]).toMatchObject({ noTest: false, bytes: 2 });
    expect(rows[1]).toMatchObject({ noTest: true, reason: 'why', refs: ['R1'], content: 'abc' });
  });

  it('falls back to the snapshot file paths', () => {
    const rows = buildFileChanges(null, [{ id: 5, tool: 't', status: 'HELD', file_path: 'x/a.py', refs: ['R2'] }, { id: 6, tool: 't', status: 'HELD' }]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ path: 'x/a.py', status: 'HELD', content: null, refs: ['R2'] });
  });

  it('links requirements to constraints, checks and files; legacy has none', () => {
    const fd = { constraints: ['c0', 'c1'], constraint_refs: [['R1'], ['R1', 'R2']], checks: ['k0'], check_refs: [['R2']] };
    const files = buildFileChanges(null, [{ id: 1, tool: 't', status: 'A', file_path: 'a.py', refs: ['R1'] }]);
    expect(requirementLinks('R1', fd, files)).toEqual({ constraints: [0, 1], checks: [], files: ['a.py'] });
    expect(requirementLinks('R2', fd, files)).toEqual({ constraints: [1], checks: [0], files: [] });
    expect(hasTraceRefs(fd)).toBe(true);
    expect(hasTraceRefs({ checks: ['k'] })).toBe(false);
  });
});

describe('team list helpers', () => {
  const team = (id: string, extra: Partial<TeamEntry['data']> = {}, name = id): TeamEntry => ({
    id,
    name,
    source: 'custom',
    data: { id, ...extra },
    errors: [],
    warnings: [],
  });
  const a = team('a', { category: 'operations', created: '2026-10-01T00:00:00Z', modified: '2026-10-18T00:00:00Z' });
  const b = team('b', { category: 'development', created: '2026-10-10T00:00:00Z' });
  const c = team('c', { category: 'not-a-category' });
  const d = team('d', { created: 'garbage' }, '0-first');

  it('sorts newest first and puts undated teams last, ties by id', () => {
    expect(sortTeams([c, a, d, b], 'created').map((t) => t.id)).toEqual(['b', 'a', 'c', 'd']);
  });

  it('sorts by modified date, falling back to the created date', () => {
    expect(sortTeams([a, b], 'modified').map((t) => t.id)).toEqual(['a', 'b']); // a edited after b was created
    expect(sortTeams([b, a], 'created').map((t) => t.id)).toEqual(['b', 'a']);
  });

  it('sorts by displayed name and does not mutate the input', () => {
    const input = [b, d, a];
    expect(sortTeams(input, 'name').map((t) => t.id)).toEqual(['d', 'a', 'b']);
    expect(input.map((t) => t.id)).toEqual(['b', 'd', 'a']);
  });

  it('groups in fixed category order, unknown or missing categories last, empty groups dropped', () => {
    expect(teamCategory(c)).toBe('other');
    expect(teamCategory(d)).toBe('other');
    const groups = groupTeams(sortTeams([c, a, d, b], 'created'));
    expect(groups.map((g) => [g.category, g.teams.map((t) => t.id)])).toEqual([
      ['development', ['b']],
      ['operations', ['a']],
      ['other', ['c', 'd']],
    ]);
  });

  it('carries keys the editor does not model through a member round trip', () => {
    const stored = { persona: 'c', providers: ['codex'], host: 'worker:any', code_tools: 'mcp' as const, note: 'kept' };
    const m = normMember(stored);
    expect(m.host).toBe('worker:any');
    expect(m.extra).toEqual({ note: 'kept' });
    expect(memberData({ ...m, persona: 'other' })).toEqual({ note: 'kept', persona: 'other', providers: ['codex'], code_tools: 'mcp', host: 'worker:any' });
    expect(normMember({ persona: 'c' })).not.toHaveProperty('extra');
    expect(memberData(normMember({ persona: 'c' }))).toEqual({ persona: 'c', providers: [] });
  });

  it('drops host when it is cleared and ignores an empty or non-string one', () => {
    const m = normMember({ persona: 'c', host: 'worker:any' });
    expect(memberData({ ...m, host: undefined })).toEqual({ persona: 'c', providers: [] });
    expect(normMember({ persona: 'c', host: '' })).not.toHaveProperty('host');
    expect(normMember({ persona: 'c', host: 3 as unknown as string })).not.toHaveProperty('host');
  });

  it('keeps host on members inside a normalized team', () => {
    const t = normalizeTeam({ id: 'x', expert: 'e', critic: [{ persona: 'c', host: 'worker:other' }], judge: 'j' });
    expect(t.critic[0]).toEqual({ persona: 'c', providers: [], host: 'worker:other' });
  });

  it('lists the team keys the editor leaves alone and leaves out the ones it owns', () => {
    const sides = [{ persona: 'a', stance: 'x' }, { persona: 'b', stance: 'y' }];
    expect(teamExtras({ id: 'x', name: 'n', expert: 'e', critic: 'c', judge: 'j', category: 'general', created: 't', modified: 't', flow: 'versus', sides })).toEqual({ flow: 'versus', sides });
    expect(teamExtras({ id: 'x', max_rounds: 2, allowed_tools: [], context: { paths: [] } })).toEqual({});
  });

  it('keeps the category when normalizing a team file', () => {
    expect(normalizeTeam({ id: 'x', category: 'operations' }).category).toBe('operations');
    expect(normalizeTeam({ id: 'x' })).not.toHaveProperty('category');
  });
});

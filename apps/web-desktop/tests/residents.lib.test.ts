import { describe, expect, it } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { ResidentTask } from '../src/types';
import {
  agentStateLabel,
  agentStateTone,
  detailPairs,
  elapsedSince,
  mailStateLabel,
  mailStateTone,
  shortSha,
  taskStatusLabel,
  turnDuration,
} from '../src/pages/residents/format';
import { boardColumns } from '../src/pages/residents/TaskBoard';

describe('resident monitor format helpers', () => {
  it('maps agent states to tones; unknown and missing states read neutral', () => {
    expect(agentStateTone('ready')).toBe('ok');
    expect(agentStateTone('busy')).toBe('info');
    expect(agentStateTone('binding')).toBe('info');
    expect(agentStateTone('retiring')).toBe('warn');
    expect(agentStateTone('down')).toBe('crit');
    expect(agentStateTone('fresh-state')).toBe('idle');
    expect(agentStateTone(null)).toBe('idle');
  });

  it('labels agent states through the catalog and keeps unknown values as they are', () => {
    expect(agentStateLabel('ready')).toBe(ko.residents.agentState.ready);
    expect(agentStateLabel('retiring')).toBe(ko.residents.agentState.retiring);
    expect(agentStateLabel('fresh-state')).toBe('fresh-state');
    expect(agentStateLabel(null)).toBe(ko.common.unknown);
  });

  it('maps mail states to tones and labels', () => {
    expect(mailStateTone('retrieved')).toBe('ok');
    expect(mailStateTone('queued')).toBe('info');
    expect(mailStateTone('delivered')).toBe('info');
    expect(mailStateTone('expired')).toBe('crit');
    expect(mailStateTone('archived')).toBe('idle');
    expect(mailStateLabel('queued')).toBe(ko.residents.mailState.queued);
    expect(mailStateLabel('archived')).toBe('archived');
    expect(mailStateLabel(null)).toBe(ko.common.unknown);
  });

  it('labels task statuses and keeps unknown values as they are', () => {
    expect(taskStatusLabel('in_progress')).toBe(ko.residents.taskStatus.in_progress);
    expect(taskStatusLabel('archived')).toBe('archived');
    expect(taskStatusLabel(null)).toBe(ko.common.unknown);
  });

  it('formats the elapsed time of an open turn', () => {
    const now = Date.parse('2026-11-21T12:00:00Z');
    expect(elapsedSince('2026-11-21T11:59:30Z', now)).toBe(ko.debate.lib.durationS(30));
    expect(elapsedSince('2026-11-21T11:58:00Z', now)).toBe(ko.debate.lib.durationMS(2, 0));
    // An unparseable or missing start is not a zero duration.
    expect(elapsedSince(null, now)).toBe(ko.common.dash);
    expect(elapsedSince('not a time', now)).toBe(ko.common.dash);
  });

  it('formats the length of a finished turn', () => {
    expect(turnDuration('2026-11-21T11:59:00Z', '2026-11-21T12:00:45Z')).toBe(ko.debate.lib.durationMS(1, 45));
    expect(turnDuration('2026-11-21T11:59:00Z', null)).toBe(ko.common.dash);
    expect(turnDuration(null, '2026-11-21T12:00:45Z')).toBe(ko.common.dash);
    // A row whose end precedes its start (clock skew) is unknown, not negative.
    expect(turnDuration('2026-11-21T12:00:45Z', '2026-11-21T11:59:00Z')).toBe(ko.common.dash);
  });

  it('shortens commit shas to the form git shows', () => {
    expect(shortSha('0123456789abcdef')).toBe('0123456');
    expect(shortSha('0123456')).toBe('0123456');
    expect(shortSha('')).toBe('');
    expect(shortSha(null)).toBe('');
  });

  it('renders event detail as compact key=value pairs', () => {
    expect(detailPairs({ task: 'k-2', action: 41, files: 2, bytes: 512 })).toBe('task=k-2 action=41 files=2 bytes=512');
    expect(detailPairs({ nested: { a: 1 } })).toBe('nested={"a":1}');
    // Empty values carry nothing; so does no detail at all.
    expect(detailPairs({ task: 'k-2', base: null, files: 0, note: '' })).toBe('task=k-2 files=0');
    expect(detailPairs({})).toBe('');
    expect(detailPairs(null)).toBe('');
  });
});

describe('resident task board columns', () => {
  const task = (id: string, status: string): ResidentTask => ({ id, status, title: id } as ResidentTask);

  it('groups tasks by status in board order', () => {
    const columns = boardColumns([task('k-4', 'completed'), task('k-1', 'pending'), task('k-2', 'in_progress'), task('k-3', 'pending')]);
    expect(columns.map((c) => c.key)).toEqual(['pending', 'in_progress', 'completed']);
    expect(columns[0].tasks.map((t) => t.id)).toEqual(['k-1', 'k-3']);
  });

  it('collects unknown statuses into one other column', () => {
    const columns = boardColumns([task('k-1', 'pending'), task('k-9', 'archived'), task('k-8', 'dormant')]);
    expect(columns.map((c) => c.key)).toEqual(['pending', 'other']);
    expect(columns[1].label).toBe(ko.residents.otherStatus);
    expect(columns[1].tasks).toHaveLength(2);
  });

  it('is empty without tasks', () => {
    expect(boardColumns([])).toEqual([]);
  });
});

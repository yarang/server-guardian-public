import { describe, expect, it } from 'vitest';
import { buildParams, paramDefaults, paramLabel, paramMax } from '../src/lib/flowTemplates';
import type { FlowTemplateData } from '../src/types';

const data = (over: Partial<FlowTemplateData> = {}): FlowTemplateData => ({
  id: 'ft',
  name: 'Template',
  projects: ['code'],
  steps: [{ kind: 'versus' }],
  task: '${TOPIC}',
  params: {
    TOPIC: { type: 'text', label: 'Question' },
    SIDE: { type: 'string', default: 'left', max: 10 },
    N: { type: 'int', min: 1, max: 5, default: 2 },
    MODE: { type: 'enum', values: ['fast', 'slow'], default: 'fast' },
  },
  ...over,
});

describe('parameter helpers', () => {
  it('labels, limits and defaults', () => {
    const d = data();
    expect(paramLabel('TOPIC', d.params!.TOPIC)).toBe('Question');
    expect(paramLabel('SIDE', d.params!.SIDE)).toBe('SIDE');
    expect(paramMax(d.params!.TOPIC)).toBe(4000);
    expect(paramMax({ type: 'string' })).toBe(400);
    expect(paramMax(d.params!.SIDE)).toBe(10);
    expect(paramMax(d.params!.N)).toBeUndefined();
    expect(paramDefaults(d)).toEqual({ TOPIC: '', SIDE: 'left', N: '2', MODE: 'fast' });
  });
});

describe('buildParams', () => {
  const d = data();

  it('sends trimmed values and leaves out an empty value that has a default', () => {
    expect(buildParams(d, { TOPIC: '  why?  ', SIDE: '', N: '3', MODE: 'slow' })).toEqual({ params: { TOPIC: 'why?', N: '3', MODE: 'slow' } });
  });

  it('names the first problem', () => {
    expect(buildParams(d, { TOPIC: '   ' })).toEqual({ issue: { code: 'required', name: 'TOPIC' } });
    expect(buildParams(d, { TOPIC: 'x', SIDE: 'a'.repeat(11) })).toEqual({ issue: { code: 'tooLong', name: 'SIDE', max: 10 } });
    expect(buildParams(d, { TOPIC: 'x', SIDE: 'a\nb' })).toEqual({ issue: { code: 'oneLine', name: 'SIDE' } });
    expect(buildParams(d, { TOPIC: 'x', N: 'two' })).toEqual({ issue: { code: 'notInt', name: 'N' } });
    expect(buildParams(d, { TOPIC: 'x', N: '9' })).toEqual({ issue: { code: 'range', name: 'N', min: 1, max: 5 } });
    expect(buildParams(d, { TOPIC: 'x', MODE: 'turbo' })).toEqual({ issue: { code: 'notOption', name: 'MODE' } });
  });

  it('requires an enum without a default', () => {
    const noDefault = data({ params: { MODE: { type: 'enum', values: ['a'] } } });
    expect(buildParams(noDefault, {})).toEqual({ issue: { code: 'required', name: 'MODE' } });
    expect(buildParams(noDefault, { MODE: 'a' })).toEqual({ params: { MODE: 'a' } });
  });

  it('sends nothing for a template without parameters', () => {
    expect(buildParams(data({ params: undefined }), {})).toEqual({ params: {} });
  });
});

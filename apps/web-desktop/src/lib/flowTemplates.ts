import type { FlowParamSpec, FlowTemplateData } from '../types';

/** Defaults of cli/llm-agent/flow_templates.py: a `string` parameter is one line of at most 400 characters, a `text` one 4000. */
export const STRING_MAX = 400;
export const TEXT_MAX = 4000;

/** Why a parameter value cannot be sent. The form turns it into a sentence (`tr.debate.start.template`). */
export type ParamIssue =
  | { code: 'required'; name: string }
  | { code: 'tooLong'; name: string; max: number }
  | { code: 'oneLine'; name: string }
  | { code: 'notInt'; name: string }
  | { code: 'range'; name: string; min?: number; max?: number }
  | { code: 'notOption'; name: string };

/** What the form shows for a parameter: its label, else its name. */
export function paramLabel(name: string, spec: FlowParamSpec): string {
  return spec.label?.trim() || name;
}

/** The longest value a parameter takes, for the field's `maxLength`. */
export function paramMax(spec: FlowParamSpec): number | undefined {
  if (spec.type === 'enum' || spec.type === 'int') return undefined;
  return spec.max ?? (spec.type === 'string' ? STRING_MAX : TEXT_MAX);
}

/** Initial form values: each parameter's default, else empty (an enum starts on its first value only when it has a default). */
export function paramDefaults(data: Pick<FlowTemplateData, 'params'>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, spec] of Object.entries(data.params ?? {})) {
    out[name] = spec.default === undefined ? '' : String(spec.default);
  }
  return out;
}

/** The first problem of one value, mirroring flow_templates._coerce so the form says it before the hub does. */
export function checkOne(name: string, spec: FlowParamSpec, raw: string): ParamIssue | null {
  if (spec.type === 'enum') {
    return (spec.values ?? []).includes(raw) ? null : { code: 'notOption', name };
  }
  const text = raw.trim();
  if (spec.type === 'int') {
    if (!/^[+-]?\d+$/.test(text)) return { code: 'notInt', name };
    const n = Number(text);
    const lo = spec.min ?? -1e9;
    const hi = spec.max ?? 1e9;
    return n < lo || n > hi ? { code: 'range', name, min: spec.min, max: spec.max } : null;
  }
  if (!text) return { code: 'required', name };
  const max = paramMax(spec) ?? TEXT_MAX;
  if ([...text].length > max) return { code: 'tooLong', name, max };
  if (spec.type === 'string' && /[\r\n]/.test(text)) return { code: 'oneLine', name };
  return null;
}

/**
 * The parameters to send, or the first problem. An empty value of a parameter with a default is left out so the
 * template's default applies; an empty one without a default is `required`. The hub (Python) stays the validator.
 */
export function buildParams(
  data: Pick<FlowTemplateData, 'params'>,
  values: Record<string, string>,
): { params: Record<string, string> } | { issue: ParamIssue } {
  const params: Record<string, string> = {};
  for (const [name, spec] of Object.entries(data.params ?? {})) {
    const raw = values[name] ?? '';
    if (spec.type !== 'enum' && !raw.trim() && spec.default !== undefined) continue;
    if (spec.type === 'enum' && !raw && spec.default !== undefined) continue;
    const issue = spec.type === 'enum' && !raw ? ({ code: 'required', name } as ParamIssue) : checkOne(name, spec, raw);
    if (issue) return { issue };
    params[name] = spec.type === 'enum' ? raw : raw.trim();
  }
  return { params };
}

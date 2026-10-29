import { tr } from '../i18n';
import { hrefFor } from '../router';
import type { PersonaConfig, TeamEntry, TeamMemberRaw } from '../types';

/** The three classic role labels (the engine and the team editor know them); builtin personas use others too. */
export const BASE_ROLES = ['Domain Expert Agent', 'Critic Agent', 'JEV-MCP Decision Agent'] as const;

/** Path param meaning "a blank form" (`#/debates/personas/new`). */
export const NEW_PERSONA_PARAM = 'new';

/** Editable form state: every field as the operator sees it (arrays as one-per-line text). */
export interface PersonaDraft {
  id: string;
  name: string;
  role: string;
  domain: string;
  prompt: string;
  focusAreas: string;
  critiqueFocus: string;
  tools: string[];
}

export type TemplateKey = 'expert' | 'critic' | 'judge';

const TEMPLATE_DEFAULTS: Record<TemplateKey, { role: string }> = {
  expert: { role: BASE_ROLES[0] },
  critic: { role: BASE_ROLES[1] },
  judge: { role: BASE_ROLES[2] },
};

export const TEMPLATE_KEYS: TemplateKey[] = ['expert', 'critic', 'judge'];

export function blankDraft(): PersonaDraft {
  return { id: '', name: '', role: BASE_ROLES[0], domain: '', prompt: '', focusAreas: '', critiqueFocus: '', tools: [] };
}

export function joinLines(items: string[] | undefined): string {
  return (items ?? []).join('\n');
}

/** One item per non-empty line, trimmed. */
export function splitLines(text: string): string[] {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l !== '');
}

export function draftFromPersona(p: PersonaConfig): PersonaDraft {
  return {
    id: p.id,
    name: p.name,
    role: p.role,
    domain: p.domain,
    prompt: p.system_prompt,
    focusAreas: joinLines(p.focus_areas),
    critiqueFocus: joinLines(p.critique_focus),
    tools: [...(p.mcp_capabilities ?? [])],
  };
}

/** "Start from": the chosen persona's content as a new persona (the id is cleared, the name is kept). */
export function draftFromCopy(p: PersonaConfig): PersonaDraft {
  return { ...draftFromPersona(p), id: '' };
}

/** A starter template: role and the skeleton text of the active language. */
export function draftFromTemplate(key: TemplateKey, current: PersonaDraft): PersonaDraft {
  const t = tr.personas.template[key];
  const d = TEMPLATE_DEFAULTS[key];
  return {
    ...current,
    name: t.name,
    role: d.role,
    domain: t.domain,
    prompt: t.prompt,
  };
}

/**
 * The payload of a save. List fields are sent when filled, and also when the persona already had the field
 * (so clearing a list really clears it); otherwise they stay absent from the file.
 */
export function personaPayload(draft: PersonaDraft, original?: PersonaConfig): PersonaConfig {
  const payload: PersonaConfig = {
    id: draft.id.trim(),
    name: draft.name,
    role: draft.role,
    domain: draft.domain,
    system_prompt: draft.prompt,
  };
  const focus = splitLines(draft.focusAreas);
  const critique = splitLines(draft.critiqueFocus);
  if (focus.length > 0 || original?.focus_areas !== undefined) payload.focus_areas = focus;
  if (critique.length > 0 || original?.critique_focus !== undefined) payload.critique_focus = critique;
  if (draft.tools.length > 0 || original?.mcp_capabilities !== undefined) payload.mcp_capabilities = [...draft.tools];
  return payload;
}

/** Role labels for the datalist: the three classic ones, then every other label found in the personas. */
export function roleOptions(personas: PersonaConfig[], extra?: string): string[] {
  const seen = new Set<string>(BASE_ROLES);
  const out: string[] = [...BASE_ROLES];
  for (const r of [...personas.map((p) => p.role), extra ?? '']) {
    if (r && !seen.has(r)) {
      seen.add(r);
      out.push(r);
    }
  }
  return out;
}

/** Case-insensitive match on name, id and domain. */
export function matchesPersona(p: PersonaConfig, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (q === '') return true;
  return [p.name, p.id, p.domain].some((s) => (s || '').toLowerCase().includes(q));
}

export type TeamSlot = 'expert' | 'proposer' | 'critic' | 'judge';

export interface PersonaUse {
  teamId: string;
  teamName: string;
  slots: TeamSlot[];
}

function memberId(m: TeamMemberRaw | undefined | null): string | undefined {
  if (m === undefined || m === null) return undefined;
  return typeof m === 'string' ? m : m.persona;
}

/** The teams whose expert / proposers / critic panel / judge reference the persona (members may be plain id strings). */
export function personaUsage(personaId: string, teams: TeamEntry[]): PersonaUse[] {
  const uses: PersonaUse[] = [];
  for (const t of teams) {
    const d = t.data;
    const slots: TeamSlot[] = [];
    if (memberId(d.expert) === personaId) slots.push('expert');
    if ((d.proposers ?? []).some((m) => memberId(m) === personaId)) slots.push('proposer');
    const critics = Array.isArray(d.critic) ? d.critic : d.critic === undefined ? [] : [d.critic];
    if (critics.some((m) => memberId(m) === personaId)) slots.push('critic');
    if (memberId(d.judge) === personaId) slots.push('judge');
    if (slots.length > 0) uses.push({ teamId: t.id, teamName: t.name || t.id, slots });
  }
  return uses;
}

/** Route of the persona page from the Debates path params after `personas`. */
export type PersonaRoute = { view: 'list' } | { view: 'new' } | { view: 'edit'; id: string };

export function parsePersonaRoute(params: string[]): PersonaRoute {
  const first = params[0];
  if (first === undefined || first === '') return { view: 'list' };
  if (first === NEW_PERSONA_PARAM) return { view: 'new' };
  return { view: 'edit', id: first };
}

/** Href of the persona page: the list, `new`, or one persona. */
export function personasHref(param?: string): string {
  return hrefFor('debates', param === undefined ? ['personas'] : ['personas', param]);
}

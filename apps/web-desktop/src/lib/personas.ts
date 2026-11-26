import type { Tone } from '@server-guardian/ui';
import { tr } from '../i18n';
import { hrefFor } from '../router';
import type { CharacterSeat, PersonaConfig, TeamEntry, TeamMemberRaw } from '../types';

/** The three classic role labels (the engine and the team editor know them); builtin personas use others too. */
export const BASE_ROLES = ['Domain Expert Agent', 'Critic Agent', 'JEV-MCP Decision Agent'] as const;

/** True for a character file (format 2, agent-characters.md phase C2): field-by-field persona, seats, no free text. */
export function isCharacter(p: PersonaConfig): boolean {
  return p.format === 2;
}

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
  tagline: string;
  look: string;
  /** role_card of the persona ('' when it has none): kept on save, since the text leaves out the seat rules. */
  roleCard: '' | 'expert' | 'critic' | 'judge';
  /** role_card_kind of the persona ('' for the code cards), kept with roleCard. */
  roleCardKind: '' | 'ops';
}

export type TemplateKey = 'expert' | 'critic' | 'judge';

const TEMPLATE_DEFAULTS: Record<TemplateKey, { role: string }> = {
  expert: { role: BASE_ROLES[0] },
  critic: { role: BASE_ROLES[1] },
  judge: { role: BASE_ROLES[2] },
};

export const TEMPLATE_KEYS: TemplateKey[] = ['expert', 'critic', 'judge'];

export function blankDraft(): PersonaDraft {
  return { id: '', name: '', role: BASE_ROLES[0], domain: '', prompt: '', focusAreas: '', critiqueFocus: '', tools: [], tagline: '', look: '', roleCard: '', roleCardKind: '' };
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

/**
 * Format 1 only. A character (format 2) has none of the free-text fields, so it cannot fill the form: the editor
 * and the dialog show a read-only view instead, and this guard turns a missed path into a visible error rather
 * than a broken draft (a character must never be saved through the format 1 editors).
 */
export function draftFromPersona(p: PersonaConfig): PersonaDraft {
  if (isCharacter(p)) throw new Error(`draftFromPersona: "${p.id}" is a character (format 2)`);
  return {
    id: p.id,
    name: p.name,
    role: p.role ?? '',
    domain: p.domain ?? '',
    prompt: p.system_prompt ?? '',
    focusAreas: joinLines(p.focus_areas),
    critiqueFocus: joinLines(p.critique_focus),
    tools: [...(p.mcp_capabilities ?? [])],
    tagline: p.profile?.tagline ?? '',
    look: p.profile?.look ?? '',
    roleCard: p.role_card ?? '',
    roleCardKind: p.role_card_kind ?? '',
  };
}

/**
 * An AI suggestion loaded into the form. An existing persona keeps its id and tools (the id cannot change and the
 * model never picks tools); a new one takes the suggested id. Nothing is saved until the person saves.
 */
export function draftFromSuggestion(s: PersonaConfig, current: PersonaDraft, isNew: boolean): PersonaDraft {
  const next = draftFromPersona(s);
  // A suggestion is a complete free text with its seat conventions (the examples carry their cards): no role card.
  return { ...next, id: isNew ? next.id : current.id, tools: current.tools, roleCard: '', roleCardKind: '' };
}

/** "Start from": the chosen persona's content as a new persona (the id is cleared, the name is kept). Format 1 only — the picker never offers a character, and `draftFromPersona` refuses one. */
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
    roleCard: '',
    roleCardKind: '',
  };
}

/**
 * The payload of a save. List fields are sent when filled, and also when the persona already had the field
 * (so clearing a list really clears it); otherwise they stay absent from the file. Format 1 only: an `original`
 * that is a character (format 2) is refused, because this payload would drop its persona block.
 */
export function personaPayload(draft: PersonaDraft, original?: PersonaConfig): PersonaConfig {
  if (original && isCharacter(original)) throw new Error(`personaPayload: "${original.id}" is a character (format 2)`);
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
  if (draft.roleCard !== '') payload.role_card = draft.roleCard;
  if (draft.roleCard !== '' && draft.roleCardKind !== '') payload.role_card_kind = draft.roleCardKind;
  const tagline = draft.tagline.trim();
  const look = draft.look.trim();
  if (tagline !== '' || look !== '' || original?.profile !== undefined) {
    payload.profile = {};
    if (tagline !== '') payload.profile.tagline = tagline;
    if (look !== '') payload.profile.look = look;
  }
  return payload;
}

/** Role labels for the datalist: the three classic ones, then every other label found in the personas (characters have none). */
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

/** Case-insensitive match on name, id and domain; a character matches its tagline and identity instead of domain. */
export function matchesPersona(p: PersonaConfig, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (q === '') return true;
  const fields = isCharacter(p) ? [p.name, p.id, p.profile?.tagline, p.persona?.identity] : [p.name, p.id, p.domain];
  return fields.some((s) => (s || '').toLowerCase().includes(q));
}

/** Seats a team editor picks personas for: the expert, critic and decision (judge) slots of a debate team. */
export type PickSeat = 'expert' | 'critic' | 'judge';

/**
 * True when a persona fits a seat: a character declares it in `seats`; a format 1 persona's role label names
 * the classic word (Expert, Critic, Decision).
 */
export function fitsSeat(p: PersonaConfig, seat: PickSeat): boolean {
  if (isCharacter(p)) return (p.seats ?? []).includes(seat);
  const label = p.role || '';
  return label.includes(seat === 'expert' ? 'Expert' : seat === 'critic' ? 'Critic' : 'Decision');
}

/** True when the typed id belongs to a character in the loaded list: a format 1 save must never overwrite it. */
export function characterIdTaken(personas: PersonaConfig[], id: string): boolean {
  const typed = id.trim();
  return typed !== '' && personas.some((p) => isCharacter(p) && p.id === typed);
}

/** Chip tone of a character seat, matching the tone the role labels get (expert info, critic warn, judge ok). */
export function seatTone(seat: CharacterSeat): Tone {
  if (seat === 'expert') return 'info';
  if (seat === 'critic') return 'warn';
  if (seat === 'judge') return 'ok';
  return 'idle';
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

/** Href of the persona (character) page in Settings -> flow setup: the list, `new`, or one persona. */
export function personasHref(param?: string): string {
  return hrefFor('settings', param === undefined ? ['personas'] : ['personas', param]);
}

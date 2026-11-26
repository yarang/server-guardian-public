import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { personaList, teamList } from './debateFixtures';
import type { PersonaConfig, PersonaList, TeamList } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { ko } from '../src/i18n/ko';
import {
  blankDraft,
  draftFromCopy,
  draftFromPersona,
  draftFromSuggestion,
  draftFromTemplate,
  fitsSeat,
  isCharacter,
  matchesPersona,
  parsePersonaRoute,
  personaPayload,
  personaUsage,
  splitLines,
} from '../src/lib/personas';
import { SettingsPage } from '../src/pages/SettingsPage';
import { parseHash, useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';

const P = ko.personas;
const mocked = vi.mocked(api);

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

/** The SettingsPage the way App renders it (`#/settings/:section/...`): the persona page is Settings -> personas. */
function Routed() {
  const params = useRoute().params;
  return <SettingsPage agentUrl="http://hub.test:9870" onEditEndpoint={() => undefined} section={params[0]} params={params.slice(1)} />;
}

async function mount(path = '') {
  window.location.hash = `#/settings${path}`;
  const view = render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle();
  return view;
}

const change = (el: Element, value: string) => fireEvent.change(el, { target: { value } });
const label = (text: string) => screen.getByLabelText(text) as HTMLInputElement;

/** A persona with every optional field, in a role label the three classic ones do not cover. */
const richPersona = {
  id: 'ops-sre',
  name: 'SRE',
  role: 'Infrastructure & SSH Guardian Agent',
  domain: 'Operations',
  system_prompt: 'You keep hosts alive.',
  focus_areas: ['disk', 'ssh'],
  critique_focus: ['blast radius'],
  mcp_capabilities: ['jev_mcp:run_tests'],
  source: 'builtin' as const,
};
const personas: PersonaList = { ...personaList, personas: [...personaList.personas, richPersona] };

/** A character file (format 2, agent-characters.md C2): field-by-field persona, seats, no format 1 fields. */
const character: PersonaConfig = {
  id: 'safety_reviewer',
  format: 2,
  name: 'Safety reviewer',
  source: 'custom',
  profile: { tagline: 'Knows what an action deletes before it runs' },
  seats: ['critic', 'verifier', 'solo'],
  kinds: ['ops'],
  role_card: 'critic',
  role_card_kind: 'ops',
  persona: {
    identity: 'a reviewer of operational changes',
    expertise: ['data loss and irreversible deletion'],
    stance: 'Cleanup is the goal.',
    checks: ['what each action deletes or stops'],
    boundaries: ['retention policy belongs to another reviewer'],
  },
  tools: [],
  rendered: '당신은 a reviewer of operational changes입니다.\n전문 분야: data loss and irreversible deletion.\n관점: Cleanup is the goal.',
};

beforeEach(() => {
  for (const fn of [mocked.fetchPersonas, mocked.savePersona, mocked.deletePersona, mocked.fetchTeams, mocked.fetchDebates, mocked.suggestPersona]) fn.mockReset();
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-04T10:00:05Z'));
  mocked.fetchPersonas.mockResolvedValue(personas);
  mocked.fetchTeams.mockResolvedValue(teamList);
  mocked.fetchDebates.mockResolvedValue([]);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('persona route', () => {
  it('parses list, new and one persona from the Settings path, and the old Debates hashes still land there', () => {
    expect(parseHash('#/settings/personas')).toEqual({ route: 'settings', params: ['personas'], query: {} });
    expect(parseHash('#/settings/personas/ops-sre').params).toEqual(['personas', 'ops-sre']);
    // Moved from Workflows (docs/design/ui-verification-review.md, M2).
    expect(parseHash('#/settings/personas')).toEqual({ route: 'settings', params: ['personas'], query: {} });
    expect(parseHash('#/settings/personas/new').params).toEqual(['personas', 'new']);
    expect(parseHash('#/settings/personas/ops-sre').params).toEqual(['personas', 'ops-sre']);
    expect(parsePersonaRoute([])).toEqual({ view: 'list' });
    expect(parsePersonaRoute(['new'])).toEqual({ view: 'new' });
    expect(parsePersonaRoute(['ops-sre'])).toEqual({ view: 'edit', id: 'ops-sre' });
  });

});

function cleanupDom() {
  document.body.innerHTML = '';
}

describe('persona list', () => {
  it('lists every persona with a builtin / custom chip and filters by search and role', async () => {
    await mount('/personas');
    expect(screen.getByText(P.listTitle(5, 5))).toBeTruthy();
    expect(screen.getAllByText(ko.debate.source.custom)).toHaveLength(1);
    expect(screen.getAllByText(ko.debate.source.builtin)).toHaveLength(4);

    // Search matches name, id and domain, case-insensitively.
    change(label(P.search), 'PERF');
    expect(screen.getByText(P.listTitle(1, 5))).toBeTruthy();
    expect(screen.getByText('Perf Critic')).toBeTruthy();
    expect(screen.queryByText('Security Expert')).toBeNull();
    change(label(P.search), 'linux hardening');
    expect(screen.getByText('Security Expert')).toBeTruthy();
    change(label(P.search), 'zzz');
    expect(screen.getByText(P.noMatch)).toBeTruthy();
    change(label(P.search), '');

    // The role filter offers the labels found in the personas, including non-classic ones.
    const filter = screen.getByLabelText(P.roleFilter) as HTMLSelectElement;
    expect([...filter.options].map((o) => o.value)).toContain('Infrastructure & SSH Guardian Agent');
    change(filter, 'Critic Agent');
    expect(screen.getByText(P.listTitle(2, 5))).toBeTruthy();
    expect(screen.getByText('Ops Critic')).toBeTruthy();
    expect(screen.queryByText('SRE')).toBeNull();
  });

  it('opens the editor of a persona and the blank form from the list', async () => {
    await mount('/personas');
    fireEvent.click(screen.getByRole('button', { name: P.editAria('SRE') }));
    await settle();
    expect(window.location.hash).toBe('#/settings/personas/ops-sre');
    expect(screen.getByRole('heading', { name: P.editTitle('SRE') })).toBeTruthy();
  });

  it('shows a load failure instead of an empty list', async () => {
    mocked.fetchPersonas.mockRejectedValue(new Error('personas exploded'));
    await mount('/personas');
    expect(screen.getByText('personas exploded')).toBeTruthy();
  });
});

describe('persona editor', () => {
  it('creates a persona with list fields split by line, and the chosen tools', async () => {
    mocked.savePersona.mockResolvedValue({ id: 'my-expert', warnings: ['prompt is short'] });
    await mount('/personas/new');
    expect(screen.getByText(ko.debate.persona.subNew)).toBeTruthy();
    expect(label(P.fields.id).readOnly).toBe(false);

    change(label(P.fields.id), ' my-expert ');
    change(label(P.fields.name), 'My Expert');
    change(label(P.fields.role), 'Release Manager');
    change(label(P.fields.domain), 'Releases');
    change(label(P.fields.systemPrompt), 'You ship.');
    change(label(P.fields.focusAreas), 'tags\n\n  changelog  \n');
    change(label(P.fields.critiqueFocus), 'rollback');
    fireEvent.click(screen.getByRole('checkbox', { name: /jev_mcp:run_tests/ }));
    fireEvent.click(screen.getByRole('button', { name: P.createButton }));
    await settle();

    expect(mocked.savePersona).toHaveBeenCalledTimes(1);
    expect(mocked.savePersona).toHaveBeenCalledWith({
      id: 'my-expert',
      name: 'My Expert',
      role: 'Release Manager',
      domain: 'Releases',
      system_prompt: 'You ship.',
      focus_areas: ['tags', 'changelog'],
      critique_focus: ['rollback'],
      mcp_capabilities: ['jev_mcp:run_tests'],
    });
    // Back on the list with the notice and the hub's warnings.
    expect(window.location.hash).toBe('#/settings/personas');
    expect(screen.getByText(P.saved('my-expert'))).toBeTruthy();
    expect(screen.getByText('⚠ prompt is short')).toBeTruthy();
  });

  it('offers the classic roles and every other role found in the personas as suggestions', async () => {
    await mount('/personas/new');
    const options = [...document.querySelectorAll('#persona-role-options option')].map((o) => (o as HTMLOptionElement).value);
    expect(options.slice(0, 3)).toEqual(['Domain Expert Agent', 'Critic Agent', 'JEV-MCP Decision Agent']);
    expect(options).toContain('Infrastructure & SSH Guardian Agent');
    expect(label(P.fields.role).getAttribute('list')).toBe('persona-role-options');
  });

  it('start from copies a persona into the form and clears the id', async () => {
    await mount('/personas/new');
    change(screen.getByLabelText(P.startFrom), 'ops-sre');
    expect(label(P.fields.id).value).toBe('');
    expect(label(P.fields.name).value).toBe('SRE');
    expect(label(P.fields.role).value).toBe('Infrastructure & SSH Guardian Agent');
    expect(label(P.fields.systemPrompt).value).toBe('You keep hosts alive.');
    expect(label(P.fields.focusAreas).value).toBe('disk\nssh');
    expect(label(P.fields.critiqueFocus).value).toBe('blast radius');
    expect((screen.getByRole('checkbox', { name: /jev_mcp:run_tests/ }) as HTMLInputElement).checked).toBe(true);
    // Source is not copied: it is a new custom persona, saved under the id typed next.
    mocked.savePersona.mockResolvedValue({ id: 'sre-2', warnings: [] });
    change(label(P.fields.id), 'sre-2');
    fireEvent.click(screen.getByRole('button', { name: P.createButton }));
    await settle();
    expect(mocked.savePersona).toHaveBeenCalledWith(expect.objectContaining({ id: 'sre-2', mcp_capabilities: ['jev_mcp:run_tests'] }));
  });

  it('fills a starter template with a role and a prompt skeleton in the catalog language', async () => {
    await mount('/personas/new');
    fireEvent.click(within(screen.getByRole('group', { name: P.templates })).getByRole('button', { name: P.template.critic.label }));
    expect(label(P.fields.role).value).toBe('Critic Agent');
    expect(label(P.fields.systemPrompt).value).toBe(P.template.critic.prompt);
    expect(label(P.fields.name).value).toBe(P.template.critic.name);
    fireEvent.click(within(screen.getByRole('group', { name: P.templates })).getByRole('button', { name: P.template.judge.label }));
    expect(label(P.fields.role).value).toBe('JEV-MCP Decision Agent');
  });

  it('shows the teams that use a persona, including plain-id members', async () => {
    await mount('/personas/perf-critic');
    const usedBy = screen.getByText(P.usedBy).parentElement as HTMLElement;
    expect(within(usedBy).getByText('Cross Review')).toBeTruthy();
    expect(within(usedBy).getByText(`(${P.slot.critic})`)).toBeTruthy();
    expect(within(usedBy).queryByText('Broken Team')).toBeNull();
    cleanupDom();

    await mount('/personas/sec-expert');
    expect(within(screen.getByText(P.usedBy).parentElement as HTMLElement).getByText(`(${P.slot.expert})`)).toBeTruthy();
    cleanupDom();

    await mount('/personas/ops-sre');
    expect(screen.getByText(P.usedByNone)).toBeTruthy();
  });

  it('says so when the team list could not be loaded, and still lets the persona be saved', async () => {
    mocked.fetchTeams.mockRejectedValue(new Error('teams exploded'));
    mocked.savePersona.mockResolvedValue({ id: 'ops-sre', warnings: [] });
    await mount('/personas/ops-sre');
    expect(screen.getByText(P.usedByUnknown)).toBeTruthy();
    // Without the catalog the persona's own saved tools are still listed.
    expect((screen.getByRole('checkbox', { name: /jev_mcp:run_tests/ }) as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.saveOverride }));
    await settle();
    expect(mocked.savePersona).toHaveBeenCalledTimes(1);
  });

  it('keeps a hub validation error on the page and does not leave it', async () => {
    mocked.savePersona.mockRejectedValue(new api.HubError('id: use 1-64 lowercase letters, digits, - or _', 400));
    await mount('/personas/new');
    change(label(P.fields.id), 'Bad Id');
    change(label(P.fields.name), 'x');
    fireEvent.click(screen.getByRole('button', { name: P.createButton }));
    await settle();
    expect(screen.getByRole('alert').textContent).toContain('id: use 1-64 lowercase letters, digits, - or _');
    expect(window.location.hash).toBe('#/settings/personas/new');
    expect(label(P.fields.id).value).toBe('Bad Id');
  });

  it('saving a builtin persona explains the override, keeps the id read-only and keeps its list fields', async () => {
    mocked.savePersona.mockResolvedValue({ id: 'ops-sre', warnings: [] });
    await mount('/personas/ops-sre');
    expect(screen.getByText(ko.debate.persona.subBuiltin)).toBeTruthy();
    expect(label(P.fields.id).readOnly).toBe(true);
    expect(screen.queryByRole('button', { name: ko.debate.persona.deleteCustom })).toBeNull();
    change(label(P.fields.focusAreas), 'disk');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.saveOverride }));
    await settle();
    expect(mocked.savePersona).toHaveBeenCalledWith({
      id: 'ops-sre',
      name: 'SRE',
      role: 'Infrastructure & SSH Guardian Agent',
      domain: 'Operations',
          system_prompt: 'You keep hosts alive.',
      focus_areas: ['disk'],
      critique_focus: ['blast radius'],
      mcp_capabilities: ['jev_mcp:run_tests'],
    });
  });

  it('deletes a custom persona only after the confirmation', async () => {
    mocked.deletePersona.mockResolvedValue(undefined);
    await mount('/personas/perf-critic');
    expect(screen.getByText(ko.debate.persona.subCustom)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.deleteCustom }));
    expect(mocked.deletePersona).not.toHaveBeenCalled();
    const confirm = screen.getByRole('dialog', { name: ko.debate.persona.deleteCustom });
    fireEvent.click(within(confirm).getByRole('button', { name: ko.debate.persona.deleteAction }));
    await settle();
    expect(mocked.deletePersona).toHaveBeenCalledWith('perf-critic');
    expect(window.location.hash).toBe('#/settings/personas');
    expect(screen.getByText(P.deleted('perf-critic'))).toBeTruthy();
  });

  it('says so for an id that does not exist', async () => {
    await mount('/personas/ghost');
    expect(screen.getByText(P.notFound('ghost'))).toBeTruthy();
  });
});

describe('persona suggestion', () => {
  const suggestion = {
    id: 'release_reviewer',
    name: '릴리스 검토자',
    role: 'Critic Agent',
    domain: '배포',
    system_prompt: '당신은 되돌리는 방법부터 묻는 검토자입니다.',
    critique_focus: ['롤백'],
    profile: { tagline: '되돌릴 수 있어야 내보낸다', look: '30대 엔지니어, 남색 셔츠' },
  };

  it('sends the brief, seat, kind and model, fills the form and saves the profile only when the person saves', async () => {
    mocked.suggestPersona.mockResolvedValue({ suggestion, warnings: ['system_prompt names a tool'], provider: 'codex-cli', examples: [] });
    mocked.savePersona.mockResolvedValue({ id: 'release_reviewer', warnings: [] });
    await mount('/personas/new');
    change(screen.getByLabelText(P.suggest.brief), '깐깐한 릴리스 검토자');
    change(screen.getByLabelText(P.suggest.seat), 'critic');
    change(screen.getByLabelText(P.suggest.kind), 'ops');
    change(screen.getByLabelText(P.suggest.provider), 'codex');
    fireEvent.click(screen.getByRole('button', { name: P.suggest.button }));
    await settle();

    expect(mocked.suggestPersona).toHaveBeenCalledWith({ brief: '깐깐한 릴리스 검토자', seat: 'critic', kind: 'ops', provider: 'codex', current: undefined });
    expect(mocked.savePersona).not.toHaveBeenCalled();
    expect(label(P.fields.id).value).toBe('release_reviewer');
    expect(label(P.fields.systemPrompt).value).toBe(suggestion.system_prompt);
    expect(label(P.fields.tagline).value).toBe('되돌릴 수 있어야 내보낸다');
    expect(screen.getByText(P.suggest.applied('codex-cli'))).toBeTruthy();
    expect(screen.getByText(/system_prompt names a tool/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: P.createButton }));
    await settle();
    expect(mocked.savePersona).toHaveBeenCalledWith(expect.objectContaining({
      id: 'release_reviewer',
      profile: { tagline: '되돌릴 수 있어야 내보낸다', look: '30대 엔지니어, 남색 셔츠' },
    }));
  });

  it('keeps the id and tools of an existing persona and sends its current draft', async () => {
    mocked.suggestPersona.mockResolvedValue({ suggestion, warnings: [], provider: 'claude-cli', examples: [] });
    await mount('/personas/ops-sre');
    change(screen.getByLabelText(P.suggest.brief), '더 짧게');
    fireEvent.click(screen.getByRole('button', { name: P.suggest.button }));
    await settle();
    expect(mocked.suggestPersona.mock.calls[0][0].current).toEqual({ name: 'SRE', domain: 'Operations', system_prompt: 'You keep hosts alive.' });
    expect(label(P.fields.id).value).toBe('ops-sre');
    expect(label(P.fields.name).value).toBe('릴리스 검토자');
    expect((screen.getByRole('checkbox', { name: /jev_mcp:run_tests/ }) as HTMLInputElement).checked).toBe(true);
  });

  it('shows the hub error and leaves the form alone', async () => {
    mocked.suggestPersona.mockRejectedValue(new Error('no LLM CLI answered'));
    await mount('/personas/new');
    change(screen.getByLabelText(P.suggest.brief), 'x');
    fireEvent.click(screen.getByRole('button', { name: P.suggest.button }));
    await settle();
    expect(screen.getByText(/no LLM CLI answered/)).toBeTruthy();
    expect(label(P.fields.systemPrompt).value).toBe('');
  });
});

describe('persona characters (format 2)', () => {
  it('knows a character by its format and searches its tagline and identity', () => {
    expect(isCharacter(character)).toBe(true);
    expect(isCharacter(richPersona)).toBe(false);
    expect(matchesPersona(character, 'SAFETY')).toBe(true);
    expect(matchesPersona(character, 'deletes before it runs')).toBe(true);
    expect(matchesPersona(character, 'operational changes')).toBe(true);
    expect(matchesPersona(character, 'zzz')).toBe(false);
    // Format 1 search keeps covering the domain.
    expect(matchesPersona(richPersona, 'operations')).toBe(true);
  });

  it('never builds a format 1 draft or save payload from a character', () => {
    expect(() => draftFromPersona(character)).toThrow(/character/);
    expect(() => draftFromCopy(character)).toThrow(/character/);
    expect(() => personaPayload(blankDraft(), character)).toThrow(/character/);
    // A format 1 persona keeps flowing through both.
    expect(personaPayload(draftFromPersona(richPersona), richPersona).id).toBe('ops-sre');
  });

  it('matches seats for the team pickers: a character by seats, a format 1 persona by its role label', () => {
    expect(fitsSeat(character, 'critic')).toBe(true);
    expect(fitsSeat(character, 'expert')).toBe(false);
    expect(fitsSeat(character, 'judge')).toBe(false);
    expect(fitsSeat(personaList.personas[0], 'expert')).toBe(true);
    expect(fitsSeat(personaList.personas[1], 'critic')).toBe(true);
    expect(fitsSeat(personaList.personas[3], 'judge')).toBe(true);
    expect(fitsSeat(richPersona, 'expert')).toBe(false);
  });

  it('lists a character with a character chip, its seats and tagline; format 1 keeps the legacy chip', async () => {
    mocked.fetchPersonas.mockResolvedValue({ ...personaList, personas: [...personaList.personas, character] });
    await mount('/personas');
    expect(screen.getByText(P.listTitle(5, 5))).toBeTruthy();
    // The Settings tab "캐릭터" uses the same word as the chip; count the chips only.
    const chips = (text: string) => screen.getAllByText(text).filter((el) => el.tagName !== 'BUTTON');
    expect(chips(P.character.chip)).toHaveLength(1);
    expect(chips(P.character.legacyChip)).toHaveLength(4);
    // The seats replace the role chip, the tagline the domain; search covers the tagline.
    expect(screen.getByText(P.slot.verifier)).toBeTruthy();
    expect(screen.getByText(character.profile?.tagline ?? '')).toBeTruthy();
    change(label(P.search), 'deletes before');
    expect(screen.getByText(P.listTitle(1, 5))).toBeTruthy();
    change(label(P.search), '');
    // The role filter offers roles only: a character has none, so it shows under "all" only.
    const filter = screen.getByLabelText(P.roleFilter) as HTMLSelectElement;
    change(filter, 'Critic Agent');
    expect(screen.getByText(P.listTitle(2, 5))).toBeTruthy();
    expect(screen.queryByText(character.profile?.tagline ?? '')).toBeNull();
  });

  it('shows a character read-only: facts and rendered text, no form, no save', async () => {
    mocked.fetchPersonas.mockResolvedValue({ ...personaList, personas: [...personaList.personas, character] });
    await mount('/personas/safety_reviewer');
    expect(screen.getByRole('heading', { name: P.character.title('Safety reviewer') })).toBeTruthy();
    expect(screen.getByText(P.character.readOnlyNotice)).toBeTruthy();
    expect(screen.getByText(character.persona?.identity ?? '')).toBeTruthy();
    expect(screen.getByText(P.slot.verifier)).toBeTruthy();
    expect(document.querySelector('pre')?.textContent).toContain('a reviewer of operational changes');
    // No format 1 form at all, and nothing that could save one.
    expect(screen.queryByLabelText(P.fields.systemPrompt)).toBeNull();
    expect(screen.queryByLabelText(P.suggest.brief)).toBeNull();
    expect(screen.queryByRole('button', { name: ko.debate.persona.saveOverride })).toBeNull();
    expect(screen.queryByRole('button', { name: ko.common.save })).toBeNull();
    // Delete is format-agnostic, so a custom character keeps it.
    expect(screen.getByRole('button', { name: ko.debate.persona.deleteCustom })).toBeTruthy();
    expect(screen.getByText(P.usedByNone)).toBeTruthy();
  });



  it('the character page never saves: no save button, savePersona not called', async () => {
    mocked.fetchPersonas.mockResolvedValue({ ...personaList, personas: [...personaList.personas, character] });
    await mount('/personas/safety_reviewer');
    expect(screen.queryByRole('button', { name: ko.common.save })).toBeNull();
    expect(screen.queryByRole('button', { name: P.createButton })).toBeNull();
    // Interacting with everything short of the confirmation saves nothing.
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.deleteCustom }));
    const confirm = screen.getByRole('dialog', { name: ko.debate.persona.deleteCustom });
    fireEvent.click(within(confirm).getByRole('button', { name: ko.shell.confirm.cancel }));
    fireEvent.click(screen.getByRole('button', { name: ko.common.close }));
    await settle();
    expect(mocked.savePersona).not.toHaveBeenCalled();
    expect(window.location.hash).toBe('#/settings/personas');
  });

  it('refuses an id that belongs to a character in the page create form; a format 1 id still saves', async () => {
    mocked.fetchPersonas.mockResolvedValue({ ...personaList, personas: [...personaList.personas, character] });
    await mount('/personas/new');
    change(label(P.fields.id), 'safety_reviewer');
    expect(screen.getByText(P.fields.idTakenByCharacter)).toBeTruthy();
    const create = screen.getByRole('button', { name: P.createButton }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    fireEvent.click(create);
    await settle();
    expect(mocked.savePersona).not.toHaveBeenCalled();
    // An id held by a format 1 persona keeps today's behaviour: saving overrides it.
    mocked.savePersona.mockResolvedValue({ id: 'perf-critic', warnings: [] });
    change(label(P.fields.id), 'perf-critic');
    expect(screen.queryByText(P.fields.idTakenByCharacter)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: P.createButton }));
    await settle();
    expect(mocked.savePersona).toHaveBeenCalledTimes(1);
    expect(mocked.savePersona).toHaveBeenCalledWith(expect.objectContaining({ id: 'perf-critic' }));
  });


  it('the refusal also holds after "Suggest with AI" fills a suggested id that belongs to a character', async () => {
    mocked.fetchPersonas.mockResolvedValue({ ...personaList, personas: [...personaList.personas, character] });
    mocked.suggestPersona.mockResolvedValue({
      suggestion: { id: 'safety_reviewer', name: '검토자', role: 'Critic Agent', domain: '검토', system_prompt: '당신은 검토자입니다.' },
      warnings: [],
      provider: 'claude-cli',
      examples: [],
    });
    await mount('/personas/new');
    change(screen.getByLabelText(P.suggest.brief), '깐깐한 검토자');
    fireEvent.click(screen.getByRole('button', { name: P.suggest.button }));
    await settle();
    expect(label(P.fields.id).value).toBe('safety_reviewer');
    expect(screen.getByText(P.fields.idTakenByCharacter)).toBeTruthy();
    expect((screen.getByRole('button', { name: P.createButton }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocked.savePersona).not.toHaveBeenCalled();
  });

  it('deletes a custom character after the confirmation', async () => {
    mocked.deletePersona.mockResolvedValue(undefined);
    mocked.fetchPersonas.mockResolvedValue({ ...personaList, personas: [...personaList.personas, character] });
    await mount('/personas/safety_reviewer');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.deleteCustom }));
    const confirm = screen.getByRole('dialog', { name: ko.debate.persona.deleteCustom });
    fireEvent.click(within(confirm).getByRole('button', { name: ko.debate.persona.deleteAction }));
    await settle();
    expect(mocked.deletePersona).toHaveBeenCalledWith('safety_reviewer');
  });

  it('a builtin character has no delete button', async () => {
    const builtin = { ...character, source: 'builtin' as const };
    mocked.fetchPersonas.mockResolvedValue({ ...personaList, personas: [...personaList.personas, builtin] });
    await mount('/personas/safety_reviewer');
    expect(screen.getByText(P.character.title('Safety reviewer'))).toBeTruthy();
    expect(screen.getByText(ko.debate.source.builtin)).toBeTruthy();
    expect(screen.queryByRole('button', { name: ko.debate.persona.deleteCustom })).toBeNull();
  });

});

describe('persona helpers', () => {
  it('splits lines, drops blanks and trims', () => {
    expect(splitLines(' a \n\n b\r\n')).toEqual(['a', 'b']);
  });

  it('omits list fields that are empty and were absent, and clears ones that existed', () => {
    const draft = { id: 'x', name: 'n', role: 'r', domain: 'd', prompt: 'p', focusAreas: '', critiqueFocus: '', tools: [], tagline: '', look: '', roleCard: '' as const, roleCardKind: '' as const };
    expect(personaPayload(draft)).toEqual({ id: 'x', name: 'n', role: 'r', domain: 'd', system_prompt: 'p' });
    expect(personaPayload(draft, richPersona)).toMatchObject({ focus_areas: [], critique_focus: [], mcp_capabilities: [] });
  });

  it('keeps role_card and role_card_kind through an edit and a copy, and drops them for a suggestion or a template', () => {
    const persona = { id: 'c', name: 'C', role: 'Critic Agent', domain: '', system_prompt: 'short', role_card: 'critic' as const };
    expect(personaPayload(draftFromPersona(persona), persona).role_card).toBe('critic');
    expect(personaPayload({ ...draftFromCopy(persona), id: 'c2' }).role_card).toBe('critic');
    const suggested = draftFromSuggestion({ ...persona, role_card: undefined, system_prompt: 'long' }, draftFromPersona(persona), false);
    expect(personaPayload(suggested).role_card).toBeUndefined();
    expect(personaPayload(draftFromTemplate('critic', draftFromPersona(persona))).role_card).toBeUndefined();
    const ops = { ...persona, role_card_kind: 'ops' as const };
    expect(personaPayload(draftFromPersona(ops), ops).role_card_kind).toBe('ops');
    expect(personaPayload({ ...draftFromCopy(ops), id: 'c2' }).role_card_kind).toBe('ops');
    expect(personaPayload(draftFromTemplate('critic', draftFromPersona(ops))).role_card_kind).toBeUndefined();
    expect(personaPayload(draftFromPersona(persona), persona).role_card_kind).toBeUndefined();
  });

  it('finds usage across expert, proposers, a single critic and the judge', () => {
    const team = (data: object) => ({ id: 't', name: 'T', source: 'custom' as const, data: { id: 't', ...data }, errors: [], warnings: [] });
    const teams: TeamList['teams'] = [
      team({ expert: 'a', proposers: ['b', { persona: 'a' }], critic: 'c', judge: { persona: 'a' } }),
    ];
    expect(personaUsage('a', teams)[0].slots).toEqual(['expert', 'proposer', 'judge']);
    expect(personaUsage('c', teams)[0].slots).toEqual(['critic']);
    expect(personaUsage('zzz', teams)).toEqual([]);
  });
});

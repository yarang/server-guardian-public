import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { personaList, teamList } from './debateFixtures';
import type { PersonaList, TeamList } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { ko } from '../src/i18n/ko';
import { parsePersonaRoute, personaPayload, personaUsage, splitLines } from '../src/lib/personas';
import { DebatesPage } from '../src/pages/DebatesPage';
import { PersonasSection } from '../src/pages/debate/PersonasSection';
import { parseHash, useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';

const P = ko.personas;
const mocked = vi.mocked(api);

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

/** The DebatesPage the way App renders it (`#/debates/:a/:b/...`). */
function Routed() {
  const params = useRoute().params;
  return <DebatesPage jobId={params[0]} wikiPage={params[1]} extra={params.slice(2)} />;
}

async function mount(path = '') {
  window.location.hash = `#/debates${path}`;
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

beforeEach(() => {
  for (const fn of [mocked.fetchPersonas, mocked.savePersona, mocked.deletePersona, mocked.fetchTeams, mocked.fetchDebates]) fn.mockReset();
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
  it('parses list, new and one persona from the Debates path', () => {
    expect(parseHash('#/debates/personas')).toEqual({ route: 'debates', params: ['personas'], query: {} });
    expect(parseHash('#/debates/personas/new').params).toEqual(['personas', 'new']);
    expect(parseHash('#/debates/personas/ops-sre').params).toEqual(['personas', 'ops-sre']);
    expect(parsePersonaRoute([])).toEqual({ view: 'list' });
    expect(parsePersonaRoute(['new'])).toEqual({ view: 'new' });
    expect(parsePersonaRoute(['ops-sre'])).toEqual({ view: 'edit', id: 'ops-sre' });
  });

  it('is linked from the Debates page and from the Settings personas section', async () => {
    await mount();
    expect(screen.getByRole('link', { name: P.link }).getAttribute('href')).toBe('#/debates/personas');
    cleanupDom();
    render(
      <GuardianRoot>
        <PersonasSection state={{ data: personas, error: null, loading: false, reload: () => undefined }} onReload={() => undefined} onChanged={() => undefined} />
      </GuardianRoot>
    );
    expect(screen.getByRole('link', { name: P.openPage }).getAttribute('href')).toBe('#/debates/personas');
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
    expect(window.location.hash).toBe('#/debates/personas/ops-sre');
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
    expect(window.location.hash).toBe('#/debates/personas');
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
    expect(window.location.hash).toBe('#/debates/personas/new');
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
    expect(window.location.hash).toBe('#/debates/personas');
    expect(screen.getByText(P.deleted('perf-critic'))).toBeTruthy();
  });

  it('says so for an id that does not exist', async () => {
    await mount('/personas/ghost');
    expect(screen.getByText(P.notFound('ghost'))).toBeTruthy();
  });
});

describe('persona helpers', () => {
  it('splits lines, drops blanks and trims', () => {
    expect(splitLines(' a \n\n b\r\n')).toEqual(['a', 'b']);
  });

  it('omits list fields that are empty and were absent, and clears ones that existed', () => {
    const draft = { id: 'x', name: 'n', role: 'r', domain: 'd', prompt: 'p', focusAreas: '', critiqueFocus: '', tools: [] };
    expect(personaPayload(draft)).toEqual({ id: 'x', name: 'n', role: 'r', domain: 'd', system_prompt: 'p' });
    expect(personaPayload(draft, richPersona)).toMatchObject({ focus_areas: [], critique_focus: [], mcp_capabilities: [] });
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

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { personaList, teamList } from './debateFixtures';
import { groupList, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { ko } from '../src/i18n/ko';
import { LOCALE } from '../src/lib/format';
import { SettingsPage, parseSection } from '../src/pages/SettingsPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);

/** Flush pending promises and timers (the tests run on fake timers so effects settle deterministically). */
const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

const onEditEndpoint = vi.fn();

/** SettingsPage the way App renders it:  becomes the section prop. */
function Routed() {
  return <SettingsPage agentUrl="http://hub.test:9870" onEditEndpoint={onEditEndpoint} section={useRoute().params[0]} />;
}

async function mount(section?: string) {
  window.location.hash = section ? `#/settings/${section}` : '#/settings';
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

beforeEach(() => {
  for (const fn of [
    mocked.fetchPersonas, mocked.savePersona, mocked.deletePersona, mocked.fetchTeams, mocked.saveTeam, mocked.deleteTeam,
    mocked.fetchServers, mocked.fetchGroups, mocked.fetchWorkers,
  ]) {
    fn.mockReset();
  }
  onEditEndpoint.mockReset();
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-04T10:00:05Z'));
  mocked.fetchPersonas.mockResolvedValue(personaList);
  mocked.fetchTeams.mockResolvedValue(teamList);
  mocked.fetchServers.mockResolvedValue(servers);
  mocked.fetchGroups.mockResolvedValue(groupList);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Settings sections', () => {
  it('defaults to the agent endpoint and opens the endpoint dialog from there', async () => {
    await mount();
    expect(screen.getByText('http://hub.test:9870')).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.endpoint }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: ko.shell.settingsPage.changeEndpoint }));
    expect(onEditEndpoint).toHaveBeenCalledTimes(1);
    // Data sections fetch only when shown.
    expect(mocked.fetchPersonas).not.toHaveBeenCalled();
    expect(mocked.fetchTeams).not.toHaveBeenCalled();
  });

  it('falls back to the agent endpoint for an unknown section', async () => {
    await mount('bogus');
    expect(screen.getByText('http://hub.test:9870')).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.endpoint }).getAttribute('aria-pressed')).toBe('true');
    expect(parseSection('bogus')).toBe('endpoint');
    expect(parseSection(undefined)).toBe('endpoint');
    // Node groups moved to the Fleet Groups tab.
    expect(parseSection('groups')).toBe('endpoint');
  });

  it('switches sections through the URL and shows each one', async () => {
    await mount();
    const nav = screen.getByRole('group', { name: ko.shell.settingsPage.sectionsAria });
    const pick = async (name: string) => {
      fireEvent.click(within(nav).getByRole('button', { name }));
      await settle();
    };

    await pick(ko.shell.settingsPage.sections.personas);
    expect(window.location.hash).toBe('#/settings/personas');
    expect(screen.getByText(ko.debate.persona.sectionCount(4))).toBeTruthy();

    await pick(ko.shell.settingsPage.sections.teams);
    expect(window.location.hash).toBe('#/settings/teams');
    expect(screen.getByText(ko.debate.team.sectionCount(2))).toBeTruthy();

    await pick(ko.shell.settingsPage.sections.projects);
    expect(window.location.hash).toBe('#/settings/projects');
    expect(screen.getByRole('button', { name: ko.debate.project.newButton })).toBeTruthy();

    expect(within(nav).queryByRole('button', { name: /그룹/ })).toBeNull();
    expect(screen.getByRole('link', { name: ko.shell.settingsPage.groupsNoteLink }).getAttribute('href')).toBe('#/fleet/groups');
  });
});

describe('Personas', () => {
  it('lists personas with a builtin / custom badge', async () => {
    await mount('personas');
    const card = screen.getByText('Perf Critic').closest('div[style]') as HTMLElement;
    expect(within(card.parentElement as HTMLElement).getAllByText(ko.debate.source.custom).length).toBeGreaterThan(0);
    expect(screen.getAllByText(ko.debate.source.builtin).length).toBeGreaterThan(0);
    expect(screen.getByText(ko.debate.persona.sectionCount(4))).toBeTruthy();
  });

  it('explains that saving a builtin creates an override and shows the server error inline', async () => {
    mocked.savePersona.mockRejectedValue(new api.HubError('system_prompt is too short', 400));
    await mount('personas');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.editAria('Security Expert') }));
    const dialog = screen.getByRole('dialog', { name: ko.debate.persona.editTitle('Security Expert') });
    expect(within(dialog).getByText(ko.debate.persona.subBuiltin)).toBeTruthy();
    expect((within(dialog).getByLabelText(ko.debate.persona.id) as HTMLInputElement).readOnly).toBe(true);

    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.persona.saveOverride }));
    await settle();
    expect(within(dialog).getByRole('alert').textContent).toBe('system_prompt is too short');
    expect(mocked.savePersona).toHaveBeenCalledWith(expect.objectContaining({ id: 'sec-expert' }));
    expect(screen.getByRole('dialog', { name: ko.debate.persona.editTitle('Security Expert') })).toBeTruthy();
  });

  it('shows saved warnings after closing and reloads the list', async () => {
    mocked.savePersona.mockResolvedValue({ id: 'sec-expert', warnings: ['prompt mentions an unknown tool'] });
    await mount('personas');
    const calls = mocked.fetchPersonas.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.editAria('Security Expert') }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: ko.debate.persona.saveOverride }));
    await settle();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(ko.debate.persona.saved('sec-expert'))).toBeTruthy();
    expect(screen.getByText('⚠ prompt mentions an unknown tool')).toBeTruthy();
    expect(mocked.fetchPersonas.mock.calls.length).toBeGreaterThan(calls);
  });

  it('deletes a custom persona only after confirmation', async () => {
    mocked.deletePersona.mockResolvedValue(undefined);
    await mount('personas');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.editAria('Perf Critic') }));
    const dialog = screen.getByRole('dialog', { name: ko.debate.persona.editTitle('Perf Critic') });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.persona.deleteCustom }));
    expect(mocked.deletePersona).not.toHaveBeenCalled();
    const confirm = screen.getByRole('dialog', { name: ko.debate.persona.deleteCustom });
    expect(within(confirm).getByText(ko.debate.persona.deleteConfirm('perf-critic'))).toBeTruthy();
    fireEvent.click(within(confirm).getByRole('button', { name: ko.debate.persona.deleteAction }));
    await settle();
    expect(mocked.deletePersona).toHaveBeenCalledWith('perf-critic');
    expect(screen.getByText(ko.debate.persona.deleted('perf-critic'))).toBeTruthy();
  });

  it('offers no delete for a builtin persona', async () => {
    await mount('personas');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.persona.editAria('Security Expert') }));
    expect(screen.queryByRole('button', { name: ko.debate.persona.deleteCustom })).toBeNull();
  });

  it('shows a load failure instead of an empty list', async () => {
    mocked.fetchPersonas.mockRejectedValue(new Error('personas exploded'));
    await mount('personas');
    expect(screen.getByText('personas exploded')).toBeTruthy();
    expect(screen.queryByText('Security Expert')).toBeNull();
  });
});

describe('Teams', () => {
  it('shows lineup on demand, source, errors in red, warnings in amber and the code path count', async () => {
    await mount('teams');
    expect(screen.getByText(ko.debate.team.sectionCount(2))).toBeTruthy();
    expect(screen.queryByText(new RegExp('^' + ko.debate.team.expert + ':'))).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.expandAria('cross-review') }));
    const lines = screen.getAllByText(new RegExp('^' + ko.debate.team.expert + ':')).map((e) => e.textContent);
    expect(lines).toContain(`${ko.debate.team.expert}: sec-expert [claude → codex]`);
    expect(screen.getByText(new RegExp('^' + ko.debate.team.criticN(2) + ':')).textContent).toBe(`${ko.debate.team.criticN(2)}: perf-critic [${ko.debate.team.hostDefault}]`);
    expect(screen.getByText(ko.debate.team.codePaths(2))).toBeTruthy();
    expect(screen.getByText('✗ unknown persona: ghost')).toBeTruthy();
    expect(screen.getByText('⚠ codex is not installed on the hub')).toBeTruthy();
    expect(screen.getByText(ko.debate.team.errorsChip(1))).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.debate.team.deleteAria('broken') })).toBeTruthy();
    expect(screen.queryByRole('button', { name: ko.debate.team.deleteAria('cross-review') })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.collapseAria('cross-review') }));
    expect(screen.queryByText(new RegExp('^' + ko.debate.team.expert + ':'))).toBeNull();
  });

  it('groups by goal with uncategorized teams last, and lists newest first when flat', async () => {
    await mount('teams');
    const group = (key: string) => screen.getByRole('region', { name: ko.debate.team.category[key] });
    expect(within(group('development')).getByText('Cross Review')).toBeTruthy();
    expect(within(group('other')).getByText('Broken Team')).toBeTruthy();
    expect(screen.getByText(ko.debate.team.groupCount(ko.debate.team.category.development, 1))).toBeTruthy();
    expect(screen.getByText(ko.debate.team.created(new Date('2026-10-04T12:00:00+09:00').toLocaleDateString(LOCALE)), { exact: false })).toBeTruthy();
    expect(screen.getByText(ko.debate.team.noDate, { exact: false })).toBeTruthy();

    const order = () => screen.getAllByRole('button', { name: /^팀 .+ 구성 (보기|접기)$/ }).map((b) => b.textContent);
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.viewFlat }));
    expect(screen.queryByRole('region', { name: ko.debate.team.category.development })).toBeNull();
    expect(order()[0]).toContain('Cross Review'); // dated team before the undated one
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.sortName }));
    expect(order()[0]).toContain('Broken Team');
  });

  const openEditor = async () => {
    await mount('teams');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.editAria('cross-review') }));
    return screen.getByRole('dialog', { name: ko.debate.teamDialog.editTitle('cross-review') });
  };

  it('keeps the id read-only when editing and marks providers that are not installed', async () => {
    const dialog = await openEditor();
    expect((within(dialog).getByLabelText(ko.debate.teamDialog.id) as HTMLInputElement).readOnly).toBe(true);
    expect(within(dialog).getByText(ko.debate.teamDialog.notInstalled)).toBeTruthy();
    expect(within(dialog).getByText(ko.debate.teamDialog.subBuiltin)).toBeTruthy();
  });

  it('requires distinct critic personas', async () => {
    const dialog = await openEditor();
    change(within(dialog).getByLabelText(ko.debate.teamDialog.personaOf(ko.debate.team.criticN(2))), 'ops-critic');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    expect(within(dialog).getByRole('alert').textContent).toBe(ko.debate.teamDialog.criticDistinct);
    expect(mocked.saveTeam).not.toHaveBeenCalled();
  });

  it('applies the critic limit from the catalog', async () => {
    const dialog = await openEditor();
    const add = within(dialog).getByRole('button', { name: ko.debate.teamDialog.addCritic }) as HTMLButtonElement;
    fireEvent.click(add);
    expect(within(dialog).getByText(ko.debate.teamDialog.criticPanel(3, 3))).toBeTruthy();
    expect(add.disabled).toBe(true);
  });

  it('checks the default reference code paths and the rounds', async () => {
    const dialog = await openEditor();
    const paths = within(dialog).getByLabelText(ko.debate.teamDialog.ctxPaths);
    const save = () => fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));

    change(paths, 'ok/*.go\n-bad');
    save();
    expect(within(dialog).getByRole('alert').textContent).toContain(ko.debate.lib.globDash('-bad'));

    change(paths, Array.from({ length: 21 }, (_, i) => `f${i}.go`).join('\n'));
    save();
    expect(within(dialog).getByRole('alert').textContent).toContain(ko.debate.lib.globsTooMany(20, 21));

    change(paths, 'ok/*.go');
    change(within(dialog).getByLabelText(ko.debate.teamDialog.maxRounds(6)), '7');
    save();
    expect(within(dialog).getByRole('alert').textContent).toBe(ko.debate.teamDialog.badRounds(6));
    expect(mocked.saveTeam).not.toHaveBeenCalled();
  });

  it('writes back the keys it does not model: a member host and the team flow and sides', async () => {
    const [first, ...rest] = teamList.teams;
    const sides = [{ persona: 'sec-expert', stance: 'for' }, { persona: 'ops-critic', stance: 'against' }];
    mocked.fetchTeams.mockResolvedValue({
      ...teamList,
      teams: [{ ...first, data: { ...first.data, flow: 'versus', sides, critic: [{ persona: 'ops-critic', providers: ['agy'], host: 'worker:any' }, 'perf-critic'] } }, ...rest],
    });
    mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: [] });
    const dialog = await openEditor();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    const sent = mocked.saveTeam.mock.calls[0][0] as Record<string, unknown>;
    expect(sent.flow).toBe('versus');
    expect(sent.sides).toEqual(sides);
    expect((sent.critic as unknown[])[0]).toEqual({ persona: 'ops-critic', providers: ['agy'], host: 'worker:any' });
    expect(sent).not.toHaveProperty('created');
  });

  it('offers no host field without workers, then lets a member be placed on a label or a worker', async () => {
    // The real catalog always carries the placement labels; without an enabled worker they must not be offered.
    mocked.fetchTeams.mockResolvedValue({
      ...teamList,
      catalog: { ...teamList.catalog, host_labels: ['worker:any', 'worker:other'], workers: [] },
    });
    let dialog = await openEditor();
    const hostLabel = ko.debate.teamDialog.hostOf(ko.debate.team.criticN(1));
    expect(within(dialog).queryByLabelText(hostLabel)).toBeNull();
    cleanup();

    mocked.fetchTeams.mockResolvedValue({
      ...teamList,
      catalog: { ...teamList.catalog, host_labels: ['worker:any', 'worker:other'], workers: ['node-1'] },
    });
    mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: [] });
    dialog = await openEditor();
    const select = within(dialog).getByLabelText(hostLabel) as HTMLSelectElement;
    expect(select.value).toBe('');
    expect([...select.options].map((o) => o.value)).toEqual(['', 'worker:any', 'worker:other', 'node-1']);
    change(select, 'node-1');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    const sent = mocked.saveTeam.mock.calls[0][0] as { critic: Array<Record<string, unknown>> };
    expect(sent.critic[0].host).toBe('node-1');
    expect(sent.critic[1]).not.toHaveProperty('host');
  });

  it('keeps a stored host the hub no longer offers selectable, flagged, and clears it on request', async () => {
    const [first, ...rest] = teamList.teams;
    mocked.fetchTeams.mockResolvedValue({
      ...teamList,
      catalog: { ...teamList.catalog, host_labels: ['worker:any'], workers: [] },
      teams: [{ ...first, data: { ...first.data, critic: [{ persona: 'ops-critic', providers: ['agy'], host: 'node-9' }, 'perf-critic'] } }, ...rest],
    });
    mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: [] });
    const dialog = await openEditor();
    const select = within(dialog).getByLabelText(ko.debate.teamDialog.hostOf(ko.debate.team.criticN(1))) as HTMLSelectElement;
    expect(select.value).toBe('node-9');
    expect(within(select).getByText(ko.debate.teamDialog.hostNotWorker('node-9'))).toBeTruthy();
    change(select, '');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    const sent = mocked.saveTeam.mock.calls[0][0] as { critic: Array<Record<string, unknown>> };
    expect(sent.critic[0]).not.toHaveProperty('host');
  });

  it('shows a member host in the team list', async () => {
    const [first, ...rest] = teamList.teams;
    mocked.fetchTeams.mockResolvedValue({
      ...teamList,
      teams: [{ ...first, data: { ...first.data, critic: [{ persona: 'ops-critic', providers: ['agy'], host: 'worker:other' }, 'perf-critic'] } }, ...rest],
    });
    await mount('teams');
    fireEvent.click(screen.getAllByRole('button', { name: new RegExp(first.id) })[0]);
    expect(screen.getByText(ko.debate.team.runsOn('worker:other'), { exact: false })).toBeTruthy();
  });

  it('saves the reordered providers, the code defaults and an explicit tool list with a high-risk warning', async () => {
    mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: ['judge persona has no tools'] });
    const dialog = await openEditor();

    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.moveUp('codex', ko.debate.team.expert) }));
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.restrict }));
    expect(within(dialog).queryByText(ko.debate.teamDialog.highRisk)).toBeNull();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /server_remediation/ }));
    expect(within(dialog).getByText(ko.debate.teamDialog.highRisk)).toBeTruthy();

    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    expect(mocked.saveTeam).toHaveBeenCalledWith({
      id: 'cross-review',
      name: 'Cross Review',
      expert: { persona: 'sec-expert', providers: ['codex', 'claude'] },
      critic: [
        { persona: 'ops-critic', providers: ['agy'] },
        { persona: 'perf-critic', providers: [] },
      ],
      judge: { persona: 'jev-judge', providers: ['claude'] },
      max_rounds: 3,
      allowed_tools: ['server_remediation'],
      context: { paths: ['apps/agent/*.go', 'cli/**/*.py'], ref: 'main' },
      category: 'development',
    });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(ko.debate.team.saved('cross-review'))).toBeTruthy();
    expect(screen.getByText('⚠ judge persona has no tools')).toBeTruthy();
  });

  it('lets the editor pick a goal category and sends it with the team', async () => {
    mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: [] });
    const dialog = await openEditor();
    const select = within(dialog).getByLabelText(ko.debate.teamDialog.category) as HTMLSelectElement;
    expect(select.value).toBe('development');
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      ko.debate.teamDialog.categoryNone,
      ...['development', 'operations', 'design-docs', 'general'].map((c) => ko.debate.team.category[c]),
    ]);
    change(select, 'operations');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    expect(mocked.saveTeam).toHaveBeenCalledWith(expect.objectContaining({ id: 'cross-review', category: 'operations' }));

    mocked.saveTeam.mockClear();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.editAria('cross-review') }));
    const again = screen.getByRole('dialog', { name: ko.debate.teamDialog.editTitle('cross-review') });
    change(within(again).getByLabelText(ko.debate.teamDialog.category), '');
    fireEvent.click(within(again).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    expect(mocked.saveTeam.mock.calls[0][0]).not.toHaveProperty('category');
  });

  const saveFrom = async (dialog: HTMLElement) => {
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    return mocked.saveTeam.mock.calls.at(-1)![0];
  };

  it('omits code_tools by default', async () => {
    mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: [] });
    const dialog = await openEditor();
    // Opt-in: nothing is checked and no explanation is shown until a member gets repository tools.
    expect(within(dialog).queryAllByText(/GUARDIAN_REPO_MCP=1/).length).toBe(0);
    for (const cb of within(dialog).getAllByLabelText(/^저장소 도구 추가/)) expect((cb as HTMLInputElement).checked).toBe(false);
    const saved = await saveFrom(dialog);
    for (const m of [saved.expert, ...saved.critic, saved.judge]) expect(m).not.toHaveProperty('code_tools');
  });

  it('sends code_tools only for members set to MCP tools', async () => {
    mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: [] });
    const dialog = await openEditor();
    fireEvent.click(within(dialog).getByLabelText(ko.debate.teamDialog.codeTools(ko.debate.team.criticN(2))));
    fireEvent.click(within(dialog).getByLabelText(ko.debate.teamDialog.codeTools(ko.debate.team.judge)));
    expect(within(dialog).getAllByText(/GUARDIAN_REPO_MCP=1/).length).toBe(2);
    const saved = await saveFrom(dialog);
    expect(saved.expert).not.toHaveProperty('code_tools');
    expect(saved.critic[0]).not.toHaveProperty('code_tools');
    expect(saved.critic[1]).toEqual({ persona: 'perf-critic', providers: [], code_tools: 'mcp' });
    expect(saved.judge).toEqual({ persona: 'jev-judge', providers: ['claude'], code_tools: 'mcp' });
  });

  describe('with a stored code_tools value', () => {
    beforeEach(() => {
      const [builtin, ...rest] = teamList.teams;
      mocked.fetchTeams.mockResolvedValue({
        ...teamList,
        teams: [{ ...builtin, data: { ...builtin.data, expert: { persona: 'sec-expert', providers: ['claude'], code_tools: 'mcp' } } }, ...rest],
      });
      mocked.saveTeam.mockResolvedValue({ id: 'cross-review', warnings: [] });
    });

    it('round-trips it', async () => {
      const dialog = await openEditor();
      expect((within(dialog).getByLabelText(ko.debate.teamDialog.codeTools(ko.debate.team.expert)) as HTMLInputElement).checked).toBe(true);
      expect((await saveFrom(dialog)).expert).toEqual({ persona: 'sec-expert', providers: ['claude'], code_tools: 'mcp' });
    });

    it('drops it when unchecked', async () => {
      const dialog = await openEditor();
      fireEvent.click(within(dialog).getByLabelText(ko.debate.teamDialog.codeTools(ko.debate.team.expert)));
      const saved = await saveFrom(dialog);
      expect(saved.expert).toEqual({ persona: 'sec-expert', providers: ['claude'] });
      expect(saved.expert).not.toHaveProperty('code_tools');
    });
  });

  it('omits allowed_tools when the judge persona tools are used and shows server errors inline', async () => {
    mocked.saveTeam.mockRejectedValue(new api.HubError('team references an unknown persona', 400));
    const dialog = await openEditor();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveCustom }));
    await settle();
    expect(mocked.saveTeam.mock.calls[0][0]).not.toHaveProperty('allowed_tools');
    expect(within(dialog).getByRole('alert').textContent).toBe('team references an unknown persona');
  });

  it('validates the id of a new team', async () => {
    await mount('teams');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.newButton }));
    const dialog = screen.getByRole('dialog', { name: ko.debate.teamDialog.newTitle });
    change(within(dialog).getByLabelText(ko.debate.teamDialog.id), 'Bad Id');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.debate.teamDialog.saveTeam }));
    expect(within(dialog).getByRole('alert').textContent).toContain(ko.debate.teamDialog.badId);
  });

  it('asks before deleting a custom team and shows the failure', async () => {
    mocked.deleteTeam.mockRejectedValue(new api.HubError('cannot delete', 500));
    await mount('teams');
    fireEvent.click(screen.getByRole('button', { name: ko.debate.team.deleteAria('broken') }));
    const confirm = screen.getByRole('dialog', { name: ko.debate.team.deleteCustom });
    fireEvent.click(within(confirm).getByRole('button', { name: ko.debate.team.deleteAction }));
    await settle();
    expect(mocked.deleteTeam).toHaveBeenCalledWith('broken');
    expect(within(confirm).getByRole('alert').textContent).toBe(ko.debate.team.deleteFailed('cannot delete'));
  });
});

describe('SettingsPage workers section', () => {
  beforeEach(() => {
    mocked.fetchWorkers.mockReset();
    vi.useFakeTimers();
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.fetchGroups.mockResolvedValue(groupList);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const report = {
    protocol: 2,
    code_version: 'abc123abc123',
    workers: [
      { id: 'node-1', name: 'node-1', enabled: true, capable: true, checked: '2026-10-22T10:00:00Z', max_jobs: 2, state: 'up' as const, error: null,
        version: 'abc123abc123', current: true, protocol: 2, free_slots: 1, providers: ['claude', 'codex'], git: true },
      { id: 'node-2', name: 'node-2', enabled: true, capable: true, checked: '2026-10-22T10:00:00Z', max_jobs: 2, state: 'up' as const, error: null,
        version: 'old000old000', current: false, protocol: 2, free_slots: 2, providers: [], git: false },
      { id: 'node-3', name: 'node-3', enabled: true, capable: true, state: 'down' as const, error: 'worker: ssh: connection refused' },
      { id: 'node-4', name: 'node-4', enabled: false, capable: true, state: 'off' as const, error: null },
    ],
  };

  it('lists every worker with its state, flags stale code and shows why a host is down', async () => {
    mocked.fetchWorkers.mockResolvedValue(report);
    await mount('workers');
    expect(screen.getAllByText(ko.debate.workers.state.up)).toHaveLength(2);
    expect(screen.getByText(ko.debate.workers.state.down)).toBeTruthy();
    expect(screen.getByText(ko.debate.workers.state.off)).toBeTruthy();
    expect(screen.getAllByText(ko.debate.workers.stale('abc123abc123'))).toHaveLength(1);
    expect(screen.getByText('worker: ssh: connection refused')).toBeTruthy();
    expect(screen.getByText(ko.debate.workers.offNote)).toBeTruthy();
    expect(screen.getByText('claude, codex')).toBeTruthy();
  });

  it('says so when the inventory has no worker hosts and reloads on request', async () => {
    mocked.fetchWorkers.mockResolvedValue({ protocol: 2, code_version: 'abc', workers: [] });
    await mount('workers');
    expect(screen.getByText(ko.debate.workers.empty)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.common.refresh }));
    await settle();
    expect(mocked.fetchWorkers).toHaveBeenCalledTimes(2);
  });

  it('shows an unreadable inventory reported in the payload', async () => {
    mocked.fetchWorkers.mockResolvedValue({ protocol: 2, code_version: 'abc', workers: [], error: 'servers.json: not found' });
    await mount('workers');
    expect(screen.getByText('servers.json: not found')).toBeTruthy();
  });
});

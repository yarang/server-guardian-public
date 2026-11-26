import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { personaList, teamList } from './debateFixtures';
import { groupList, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
// The desktop-shell bridge: the Display test checks that nothing calls it.
const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
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
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  invoke.mockReset();
});

describe('Settings sections', () => {
  it('defaults to the agent endpoint and opens the endpoint dialog from there', async () => {
    await mount();
    expect(screen.getByText('http://hub.test:9870')).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.endpoint }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: ko.shell.settingsPage.changeEndpoint }));
    expect(onEditEndpoint).toHaveBeenCalledTimes(1);
    // The language panel lives under Display; the flow switcher only exists in the flow group.
    expect(screen.queryByRole('combobox', { name: ko.shell.language.label })).toBeNull();
    expect(screen.queryByRole('group', { name: ko.shell.settingsPage.flowSectionsAria })).toBeNull();
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
    // Node groups moved to the Fleet Groups tab, command sets to the Nodes page.
    expect(parseSection('groups')).toBe('endpoint');
    expect(parseSection('commandSets')).toBe('endpoint');
  });

  it('switches the four groups and the flow sections through the URL and shows each one', async () => {
    await mount();
    const nav = screen.getByRole('group', { name: ko.shell.settingsPage.sectionsAria });
    const pick = async (scope: HTMLElement, name: string) => {
      fireEvent.click(within(scope).getByRole('button', { name }));
      await settle();
    };
    expect(within(nav).getAllByRole('button').map((b) => b.textContent)).toEqual([
      ko.shell.settingsPage.sections.endpoint,
      ko.shell.settingsPage.sections.flowConfig,
      ko.shell.settingsPage.sections.workers,
      ko.shell.settingsPage.sections.display,
    ]);

    // The flow group opens on projects and keeps its highlight across its own sections.
    await pick(nav, ko.shell.settingsPage.sections.flowConfig);
    expect(window.location.hash).toBe('#/settings/projects');
    expect(screen.getByRole('button', { name: ko.debate.project.newButton })).toBeTruthy();
    const flow = screen.getByRole('group', { name: ko.shell.settingsPage.flowSectionsAria });
    expect(within(flow).getAllByRole('button').map((b) => b.textContent)).toEqual([
      ko.shell.settingsPage.sections.projects,
      ko.shell.settingsPage.sections.flows,
      ko.shell.settingsPage.sections.personas,
    ]);

    // Flows and characters moved here from Workflows (docs/design/ui-verification-review.md, M1/M2).
    await pick(flow, ko.shell.settingsPage.sections.flows);
    expect(window.location.hash).toBe('#/settings/flows');
    expect(screen.getByText(ko.flows.intro)).toBeTruthy();

    await pick(flow, ko.shell.settingsPage.sections.personas);
    expect(window.location.hash).toBe('#/settings/personas');
    expect(screen.getByText(ko.personas.listTitle(4, 4))).toBeTruthy();
    expect(within(nav).getByRole('button', { name: ko.shell.settingsPage.sections.flowConfig }).getAttribute('aria-pressed')).toBe('true');

    expect(within(nav).queryByRole('button', { name: /그룹/ })).toBeNull();
    expect(screen.getByRole('link', { name: ko.shell.settingsPage.groupsNoteLink }).getAttribute('href')).toBe('#/fleet/groups');
  });

  it('keeps every legacy section hash working and lands on the right pane', async () => {
    mocked.fetchWorkers.mockResolvedValue({ protocol: 2, code_version: 'abc', workers: [] });
    // The flow group's own hash means its first section.
    expect(parseSection('flowConfig')).toBe('projects');

    await mount('endpoint');
    expect(screen.getByText('http://hub.test:9870')).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.endpoint }).getAttribute('aria-pressed')).toBe('true');
    cleanup();

    await mount('flowConfig');
    expect(window.location.hash).toBe('#/settings/flowConfig');
    expect(screen.getByRole('button', { name: ko.debate.project.newButton })).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.flowConfig }).getAttribute('aria-pressed')).toBe('true');
    cleanup();

    await mount('projects');
    expect(screen.getByRole('button', { name: ko.debate.project.newButton })).toBeTruthy();
    cleanup();

    // The removed team editor's hash opens the flows (X1).
    expect(parseSection('teams')).toBe('flows');
    await mount('teams');
    expect(screen.getByText(ko.flows.intro)).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.flowConfig }).getAttribute('aria-pressed')).toBe('true');
    cleanup();

    await mount('personas');
    expect(screen.getByText(ko.personas.listTitle(4, 4))).toBeTruthy();
    cleanup();

    await mount('workers');
    expect(screen.getByText(ko.debate.workers.empty)).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.workers }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.queryByRole('group', { name: ko.shell.settingsPage.flowSectionsAria })).toBeNull();
  });

  it('shows only the language under Display, in the browser and in the desktop shell (no export folder, X3)', async () => {
    await mount('display');
    expect(screen.getByRole('button', { name: ko.shell.settingsPage.sections.display }).getAttribute('aria-pressed')).toBe('true');
    expect((screen.getByRole('combobox', { name: ko.shell.language.label }) as HTMLSelectElement).value).toBe('ko');
    expect(screen.getByText(ko.shell.language.hint)).toBeTruthy();
    expect(screen.queryByText('http://hub.test:9870')).toBeNull();
    expect(screen.queryByRole('group', { name: ko.shell.settingsPage.flowSectionsAria })).toBeNull();
    cleanup();

    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    await mount('display');
    expect(screen.getByRole('combobox', { name: ko.shell.language.label })).toBeTruthy();
    expect(invoke).not.toHaveBeenCalled();
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

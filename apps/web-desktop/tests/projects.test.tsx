import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { teamList } from './debateFixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { SettingsPage } from '../src/pages/SettingsPage';
import { AppStores } from '../src/state/AppStores';
import { PROJECT_STORAGE_KEY, hasNoRepository, opsProjectFor } from '../src/lib/project';
import { setDebatePrefill } from '../src/state/debatePrefill';
import { defaultFlowChoices, formatTestCommands, parseTestCommands } from '../src/pages/debate/ProjectDialog';
import type { FlowEntry, ProjectEntry, ProjectList } from '../src/types';
import { ko } from '../src/i18n/ko';

const D = ko.debate;

const mocked = vi.mocked(api);

const projectList: ProjectList = {
  custom_dir: '/x',
  projects: [
    {
      id: 'server-guardian',
      source: 'builtin',
      data: { id: 'server-guardian', name: 'Server Guardian', repo: 'https://git.example.com/owner/server-guardian.git' },
      errors: [],
      status: { workspace: '/home/ubuntu/guardian-workspace', present: true, head: 'abcdef1234567890', branch: 'main', modified: 0 },
    },
    {
      id: 'svc',
      source: 'custom',
      data: { id: 'svc', name: 'My Service', repo: 'https://git.example.com/me/svc.git' },
      errors: [],
      status: { workspace: '/home/ubuntu/guardian-workspaces/svc', present: false },
    },
  ],
};

const fleet: ProjectEntry = {
  id: 'fleet',
  source: 'builtin',
  data: { id: 'fleet', name: 'Fleet', kind: 'ops', targets: { all: true }, policy: { allowed_actions: ['jev_mcp:server_remediation'] } },
  errors: [],
  status: { kind: 'ops', present: false },
  targets: { ids: ['n1', 'n2', 'n3'], hub: 'hub1', include_hub: false, missing: { groups: [], nodes: [] }, error: null },
};
const armOps: ProjectEntry = {
  id: 'arm-ops',
  source: 'custom',
  data: { id: 'arm-ops', name: 'ARM ops', kind: 'ops', targets: { groups: ['arm', 'gone'], exclude: ['n2'] } },
  errors: [],
  status: { kind: 'ops', present: false },
  targets: { ids: ['n1'], hub: 'hub1', include_hub: false, missing: { groups: ['gone'], nodes: [] }, error: null },
};
const withOps: ProjectList = { ...projectList, projects: [...projectList.projects, fleet, armOps] };

function mount(page: 'debate' | 'settings' = 'settings') {
  window.location.hash = page === 'settings' ? '#/settings/projects' : '#/debates';
  return render(
    <GuardianRoot>
      <AppStores>
        {page === 'settings' ? <SettingsPage agentUrl="http://hub.test:9870" onEditEndpoint={() => undefined} section="projects" /> : <DebatesPage />}
      </AppStores>
    </GuardianRoot>
  );
}

const card = (name: string) => screen.getByText(name).closest('div[style]')!.parentElement as HTMLElement;
const switcher = () => screen.getByLabelText(ko.shell.projectSwitcher.label) as HTMLSelectElement;

beforeEach(() => {
  for (const fn of [mocked.fetchProjects, mocked.syncProject, mocked.saveProject, mocked.fetchDebates, mocked.listActions, mocked.startDebate]) {
    fn.mockReset();
  }
  mocked.fetchTeams.mockResolvedValue(teamList);
  mocked.fetchProjects.mockResolvedValue(projectList);
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.listActions.mockResolvedValue([]);
});

afterEach(() => vi.restoreAllMocks());

describe('Projects (Settings)', () => {
  it('lists projects with their workspace state and no longer offers a select role', async () => {
    mount();
    expect(await screen.findByText('My Service')).toBeTruthy();
    expect(screen.getByText('main @ abcdef123456')).toBeTruthy();
    expect(screen.getByText('/home/ubuntu/guardian-workspaces/svc · ' + D.project.notCloned)).toBeTruthy();
    expect(screen.getByRole('button', { name: D.project.syncAria('svc') }).textContent).toBe(D.project.clone);
    expect(screen.getByRole('button', { name: D.project.editAria('svc') })).toBeTruthy();
    expect(screen.getByRole('button', { name: D.project.newButton })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Use project svc' })).toBeNull();
    expect(within(card('My Service')).queryByText('selected')).toBeNull();
  });

  it('clones a project and reports the result', async () => {
    mocked.syncProject.mockResolvedValue({ id: 'svc', ok: true, action: 'cloned', branch: 'main', head: '0123456789abcdef' });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.syncAria('svc') }));
    expect(await screen.findByText(`svc: ${D.project.syncAction.cloned} (main @ 0123456789ab).`)).toBeTruthy();
    expect(mocked.syncProject).toHaveBeenCalledWith('svc');
  });

  it('shows a sync failure from the hub', async () => {
    mocked.syncProject.mockRejectedValue(new Error('git clone failed: Authentication failed'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.syncAria('svc') }));
    expect(await screen.findByText('svc: git clone failed: Authentication failed')).toBeTruthy();
  });
});

describe('Project switcher', () => {
  it('starts on the default project and lists the hub projects', async () => {
    mount('debate');
    await waitFor(() => expect(Array.from(switcher().options).map((o) => o.value)).toEqual(['server-guardian', 'svc']));
    expect(switcher().value).toBe('server-guardian');
    await waitFor(() => expect(mocked.fetchDebates).toHaveBeenCalledWith('server-guardian'));
    expect(mocked.listActions).toHaveBeenCalledWith(undefined, 'server-guardian');
  });

  it('switching project refetches debates and actions, is remembered, and is sent with the next debate', async () => {
    mount('debate');
    await waitFor(() => expect(switcher().options.length).toBe(2));
    fireEvent.change(switcher(), { target: { value: 'svc' } });
    await waitFor(() => expect(mocked.fetchDebates).toHaveBeenLastCalledWith('svc'));
    await waitFor(() => expect(mocked.listActions).toHaveBeenLastCalledWith(undefined, 'svc'));
    expect(window.localStorage.getItem(PROJECT_STORAGE_KEY)).toBe('svc');

    mocked.startDebate.mockResolvedValue({ id: 'dbt_1', task: 'x', team: '', thread_id: '', rounds: 0, status: 'running', started_at: '', finished_at: null, exit_code: null, error: '' } as never);
    fireEvent.click(screen.getByRole('button', { name: D.page.newDebate }));
    fireEvent.change(screen.getByLabelText(new RegExp('^' + D.start.task)), { target: { value: 'Review the service' } });
    fireEvent.click(screen.getByRole('button', { name: D.start.start }));
    await waitFor(() => expect(mocked.startDebate).toHaveBeenCalledWith(expect.objectContaining({ project: 'svc', task: 'Review the service' })));
  });

  it('restores the remembered project on the next visit', async () => {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, 'svc');
    mount('debate');
    await waitFor(() => expect(switcher().value).toBe('svc'));
    await waitFor(() => expect(mocked.listActions).toHaveBeenLastCalledWith(undefined, 'svc'));
  });

  it('falls back to the default project when the remembered one no longer exists, keeping the stored value', async () => {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, 'gone');
    mount('debate');
    await waitFor(() => expect(Array.from(switcher().options).map((o) => o.value)).toEqual(['server-guardian', 'svc']));
    expect(switcher().value).toBe('server-guardian');
    await waitFor(() => expect(mocked.listActions).toHaveBeenLastCalledWith(undefined, 'server-guardian'));
    expect(window.localStorage.getItem(PROJECT_STORAGE_KEY)).toBe('gone');
  });

  it('keeps the remembered project while the list cannot be loaded', async () => {
    window.localStorage.setItem(PROJECT_STORAGE_KEY, 'svc');
    mocked.fetchProjects.mockRejectedValue(new Error('projects down'));
    mount('debate');
    await waitFor(() => expect(mocked.fetchProjects).toHaveBeenCalled());
    expect(switcher().value).toBe('svc');
    expect(mocked.listActions).toHaveBeenLastCalledWith(undefined, 'svc');
  });

  it('uses the default project and still switches when localStorage is unavailable', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage blocked');
    });
    mount('debate');
    await waitFor(() => expect(switcher().options.length).toBe(2));
    expect(switcher().value).toBe('server-guardian');
    fireEvent.change(switcher(), { target: { value: 'svc' } });
    await waitFor(() => expect(switcher().value).toBe('svc'));
  });

  it('lists a project with configuration errors as disabled', async () => {
    mocked.fetchProjects.mockResolvedValue({
      ...projectList,
      projects: [...projectList.projects, { id: 'bad', source: 'custom', data: { id: 'bad' }, errors: ['repo is required'] }],
    });
    mount('debate');
    await waitFor(() => expect(switcher().options.length).toBe(3));
    expect(Array.from(switcher().options).find((o) => o.value === 'bad')?.disabled).toBe(true);
  });
});

describe('Ops projects', () => {
  it('shows targets instead of a workspace and offers edit but no sync or reset', async () => {
    mocked.fetchProjects.mockResolvedValue(withOps);
    mount();
    const arm = card(await screen.findByText('ARM ops').then((el) => el.textContent ?? ''));
    expect(within(arm).getByText(D.project.opsChip)).toBeTruthy();
    expect(within(arm).getByText(D.project.opsDefinition(false, ['arm', 'gone'], [], ['n2']))).toBeTruthy();
    expect(within(arm).getByText(D.project.opsTargets(1, false))).toBeTruthy();
    expect(within(arm).getByText(D.project.opsMissing(['gone']))).toBeTruthy();
    expect(screen.queryByRole('button', { name: D.project.syncAria('arm-ops') })).toBeNull();
    expect(screen.queryByRole('button', { name: D.project.resetAria('arm-ops') })).toBeNull();
    expect(screen.getByRole('button', { name: D.project.editAria('arm-ops') })).toBeTruthy();
    expect(screen.getByRole('button', { name: D.project.syncAria('svc') })).toBeTruthy();
  });

  it('creates an ops project with targets, policy and auto_run', async () => {
    mocked.saveProject.mockResolvedValue({ id: 'arm-clean', warnings: [] });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.newButton }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(D.project.kind), { target: { value: 'ops' } });
    expect(within(dialog).queryByLabelText(D.project.repo)).toBeNull();
    fireEvent.change(within(dialog).getByLabelText(D.project.id), { target: { value: 'arm-clean' } });
    fireEvent.change(within(dialog).getByLabelText(D.project.ops.groups), { target: { value: 'arm\n' } });
    fireEvent.change(within(dialog).getByLabelText(D.project.ops.exclude), { target: { value: 'n2' } });
    fireEvent.click(within(dialog).getByLabelText('jev_mcp:server_remediation'));
    fireEvent.change(within(dialog).getByLabelText(new RegExp(D.project.ops.denyPaths)), { target: { value: '/srv/data/*' } });
    fireEvent.change(within(dialog).getByLabelText(new RegExp(D.project.ops.protectedPackages)), { target: { value: 'nginx*\ninternal-*' } });
    const autoApt = within(dialog).getAllByLabelText('ops:apt_clean')[1];
    fireEvent.click(autoApt);
    fireEvent.change(within(dialog).getByLabelText(D.project.ops.maxAutoRuns), { target: { value: '3' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await waitFor(() => expect(mocked.saveProject).toHaveBeenCalled());
    expect(mocked.saveProject.mock.calls[0][0]).toEqual({
      id: 'arm-clean',
      name: undefined,
      description: undefined,
      kind: 'ops',
      targets: { all: false, groups: ['arm'], nodes: [], exclude: ['n2'] },
      policy: {
        allowed_actions: ['ops:docker_prune', 'ops:journal_vacuum', 'ops:apt_clean', 'ops:remove_paths', 'ops:truncate_logs',
          'ops:remove_old_kernels', 'ops:restart_service'],
        include_hub: false,
        deny_paths: ['/srv/data/*'],
        deny_commands: [],
        protected_packages: ['nginx*', 'internal-*'],
        auto_run: ['ops:apt_clean'],
        max_auto_runs_per_day: 3,
        batch: { auto: false, max_plans_per_day: 0, max_steps: 20, max_parallel_hosts: 2, scripts: 'approve', script_groups: [] },
      },
    });
  });

  it('raises a daily maximum of 0 to 1 when automatic batch runs are ticked, since the hub refuses auto with 0', async () => {
    mocked.fetchProjects.mockResolvedValue(withOps);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.editAria('arm-ops') }));
    const dialog = await screen.findByRole('dialog');
    const plans = within(dialog).getByLabelText(D.project.ops.batchPlans) as HTMLInputElement;
    expect(plans.value).toBe('0');
    fireEvent.click(within(dialog).getByLabelText(D.project.ops.batchAuto));
    expect(plans.value).toBe('1');
  });

  it('edits an existing ops project without offering a change of kind', async () => {
    mocked.fetchProjects.mockResolvedValue(withOps);
    mocked.saveProject.mockResolvedValue({ id: 'arm-ops', warnings: [] });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.editAria('arm-ops') }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText(D.project.kind)).toBeNull();
    expect((within(dialog).getByLabelText(D.project.ops.groups) as HTMLTextAreaElement).value).toBe('arm\ngone');
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await waitFor(() => expect(mocked.saveProject).toHaveBeenCalled());
    expect(mocked.saveProject.mock.calls[0][0]).toMatchObject({ id: 'arm-ops', kind: 'ops', targets: { groups: ['arm', 'gone'], exclude: ['n2'] } });
  });

  it('keeps the default flow of a saved project, and changes it from the list (D7 of flow-graph.md)', async () => {
    const base = withOps.projects.find((p) => p.id === 'arm-ops')!;
    mocked.fetchProjects.mockResolvedValue({
      ...withOps,
      projects: withOps.projects.map((p) => (p.id === 'arm-ops' ? { ...base, data: { ...base.data, team: 'fleet-ops' } } : p)),
    });
    const flows = { flows: [flowEntry('ops-review', ['ops']), flowEntry('code-review', ['code'])], catalog: {} as never };
    mocked.fetchFlows.mockResolvedValue(flows);
    mocked.saveProject.mockResolvedValue({ id: 'arm-ops', warnings: [] });
    mount();
    const open = async () => {
      fireEvent.click(await screen.findByRole('button', { name: D.project.editAria('arm-ops') }));
      const dialog = await screen.findByRole('dialog');
      const select = within(dialog).getByLabelText(D.project.flow) as HTMLSelectElement;
      await waitFor(() => expect(within(select).queryByRole('option', { name: /ops-review/ })).not.toBeNull());
      return { dialog, select };
    };
    // Saved unchanged: the key is kept even though fleet-ops is not in the flow list (it stays as an unlisted option).
    let { dialog, select } = await open();
    expect(select.value).toBe('fleet-ops');
    expect(within(select).queryByRole('option', { name: /code-review/ })).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await waitFor(() => expect(mocked.saveProject).toHaveBeenCalledTimes(1));
    expect(mocked.saveProject.mock.calls[0][0].team).toBe('fleet-ops');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    ({ dialog, select } = await open());
    fireEvent.change(select, { target: { value: 'ops-review' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await waitFor(() => expect(mocked.saveProject).toHaveBeenCalledTimes(2));
    expect(mocked.saveProject.mock.calls[1][0].team).toBe('ops-review');
    mocked.fetchFlows.mockResolvedValue({ ...flows, flows: [] });
  });

  it('keeps policy.batch and policy.watch of a saved project, and edits the batch settings', async () => {
    const batch = { auto: true, max_plans_per_day: 2, max_steps: 5, max_parallel_hosts: 3 };
    const watch = { disk: true };
    const base = withOps.projects.find((p) => p.id === 'arm-ops')!;
    mocked.fetchProjects.mockResolvedValue({
      ...withOps,
      projects: withOps.projects.map((p) => (p.id === 'arm-ops' ? { ...base, data: { ...base.data, policy: { ...base.data.policy, batch, watch } } } : p)),
    });
    mocked.saveProject.mockResolvedValue({ id: 'arm-ops', warnings: [] });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.editAria('arm-ops') }));
    const dialog = await screen.findByRole('dialog');
    expect((within(dialog).getByLabelText(D.project.ops.batchAuto) as HTMLInputElement).checked).toBe(true);
    expect((within(dialog).getByLabelText(D.project.ops.batchSteps) as HTMLInputElement).value).toBe('5');
    fireEvent.change(within(dialog).getByLabelText(D.project.ops.batchPlans), { target: { value: '4' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await waitFor(() => expect(mocked.saveProject).toHaveBeenCalled());
    const saved = mocked.saveProject.mock.calls[0][0];
    expect(saved.policy?.batch).toEqual({ ...batch, max_plans_per_day: 4, scripts: 'approve', script_groups: [] });
    expect(saved.policy?.watch).toEqual(watch);
  });

  it('edits deny_commands and releases scripts by vote only on the listed groups', async () => {
    mocked.saveProject.mockResolvedValue({ id: 'x', warnings: [] });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.newButton }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(D.project.kind), { target: { value: 'ops' } });
    fireEvent.change(within(dialog).getByLabelText(D.project.id), { target: { value: 'x' } });
    fireEvent.change(within(dialog).getByLabelText(D.project.ops.groups), { target: { value: 'lab' } });
    fireEvent.click(within(dialog).getByLabelText('ops:run_script'));
    fireEvent.change(within(dialog).getByLabelText(new RegExp(D.project.ops.denyCommands)), { target: { value: 'dd *\n' } });
    // The vote needs automatic runs, so the box is off until they are on, and the group list shows only when ticked.
    expect((within(dialog).getByLabelText(D.project.ops.batchScriptsVote) as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(within(dialog).getByLabelText(D.project.ops.batchAuto));
    fireEvent.click(within(dialog).getByLabelText(D.project.ops.batchScriptsVote));
    fireEvent.change(within(dialog).getByLabelText(new RegExp(D.project.ops.scriptGroups.split(' (')[0])), { target: { value: 'lab\n' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await waitFor(() => expect(mocked.saveProject).toHaveBeenCalled());
    const policy = mocked.saveProject.mock.calls[0][0].policy;
    expect(policy?.deny_commands).toEqual(['dd *']);
    expect(policy?.batch).toMatchObject({ auto: true, scripts: 'vote', script_groups: ['lab'] });
  });

  it('offers the package and command-set tools, off for a new project', async () => {
    mocked.saveProject.mockResolvedValue({ id: 'x', warnings: [] });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: D.project.newButton }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(D.project.kind), { target: { value: 'ops' } });
    for (const tool of ['ops:package_install', 'ops:package_remove', 'ops:run_command_set', 'ops:run_script']) {
      expect((within(dialog).getByLabelText(tool) as HTMLInputElement).checked).toBe(false);
    }
    fireEvent.click(within(dialog).getByLabelText('ops:run_command_set'));
    fireEvent.change(within(dialog).getByLabelText(D.project.id), { target: { value: 'x' } });
    fireEvent.change(within(dialog).getByLabelText(D.project.ops.groups), { target: { value: 'arm' } });
    fireEvent.click(within(dialog).getByRole('button', { name: ko.common.save }));
    await waitFor(() => expect(mocked.saveProject).toHaveBeenCalled());
    expect(mocked.saveProject.mock.calls[0][0].policy?.allowed_actions).toContain('ops:run_command_set');
    expect(mocked.saveProject.mock.calls[0][0].policy?.allowed_actions).not.toContain('ops:run_script');
  });

  it('treats ops and design projects as having no repository', () => {
    const design: ProjectEntry = {
      id: 'arch-docs',
      source: 'custom',
      data: { id: 'arch-docs', name: 'Architecture docs', kind: 'design' },
      errors: [],
      status: { kind: 'design', present: false },
    } as ProjectEntry;
    expect(hasNoRepository(design)).toBe(true);
    expect(hasNoRepository(fleet)).toBe(true);
    expect(hasNoRepository(projectList.projects[0])).toBe(false);
    expect(hasNoRepository(undefined)).toBe(false);
  });

  it('picks the ops project of a node or group, else the fleet project', () => {
    expect(opsProjectFor(withOps.projects, {})).toBe('fleet');
    expect(opsProjectFor(withOps.projects, { node: 'n1' })).toBe('arm-ops');
    expect(opsProjectFor(withOps.projects, { node: 'n3' })).toBe('fleet');
    expect(opsProjectFor(withOps.projects, { group: 'arm' })).toBe('arm-ops');
    expect(opsProjectFor(withOps.projects, { group: 'x86' })).toBe('fleet');
    expect(opsProjectFor(projectList.projects, { node: 'n1' })).toBeUndefined();
  });

  it('a fleet hand-off switches to the ops project, hides the wiki and sends no reference code', async () => {
    mocked.fetchProjects.mockResolvedValue(withOps);
    mocked.startDebate.mockResolvedValue({ id: 'dbt_1', task: 'x', team: '', thread_id: '', rounds: 0, status: 'running', started_at: '', finished_at: null, exit_code: null, error: '' } as never);
    setDebatePrefill('Free disk space on the ARM nodes', 'group', { group: 'arm' });
    mount('debate');
    await waitFor(() => expect(switcher().value).toBe('arm-ops'));
    expect(screen.queryByRole('radio', { name: D.page.tabWiki })).toBeNull();
    expect(screen.queryByLabelText(new RegExp('^' + D.start.refCode))).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: D.start.start }));
    await waitFor(() => expect(mocked.startDebate).toHaveBeenCalled());
    const req = mocked.startDebate.mock.calls[0][0];
    expect(req).toEqual(expect.objectContaining({ project: 'arm-ops', task: 'Free disk space on the ARM nodes' }));
    expect(req.context_paths).toBeUndefined();
  });
});

function flowEntry(id: string, projects: Array<'code' | 'ops' | 'design'>, extra: Partial<FlowEntry['data']> = {}): FlowEntry {
  return {
    id, name: id, source: 'custom', hash: 'h', calls: 3, runnable: true, run_error: null, errors: [], warnings: [],
    data: { id, name: id, projects, seats: [], close: { character: 'judge' }, links: [], ...extra },
  };
}

describe('default flow choices', () => {
  it('offers runnable flows of the kind without a task text of their own', () => {
    const flows = [
      flowEntry('any', []),
      flowEntry('ops-only', ['ops']),
      flowEntry('code-only', ['code']),
      flowEntry('own-task', ['ops'], { task: 'Check {x}' }),
      { ...flowEntry('broken', ['ops']), runnable: false },
    ];
    expect(defaultFlowChoices(flows, 'ops').map((f) => f.id)).toEqual(['any', 'ops-only']);
    expect(defaultFlowChoices(flows, 'code').map((f) => f.id)).toEqual(['any', 'code-only']);
  });
});

describe('test command lines', () => {
  it('round-trips prefixes and flags', () => {
    const commands = parseTestCommands('cargo test: --release, -p=\n\nnpx vitest run\n');
    expect(commands).toEqual([{ prefix: 'cargo test', flags: { '--release': false, '-p': true } }, { prefix: 'npx vitest run' }]);
    expect(formatTestCommands(commands)).toBe('cargo test: --release, -p=\nnpx vitest run');
  });
});

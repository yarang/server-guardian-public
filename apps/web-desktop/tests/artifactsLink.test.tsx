import { act, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { ProjectEntry } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { ArtifactsLink } from '../src/pages/debate/ArtifactsLink';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);

const project = (id: string, kind?: 'design' | 'ops'): ProjectEntry =>
  ({ id, source: 'custom', data: { id, name: id, kind }, errors: [], status: { present: true } }) as ProjectEntry;

async function mount(props: { project?: string }) {
  render(
    <GuardianRoot>
      <AppStores>
        <ArtifactsLink thread="thread_ab1" {...props} />
      </AppStores>
    </GuardianRoot>
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(300);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.fetchProjects.mockResolvedValue({
    projects: [project('arch-docs', 'design'), project('other-design', 'design'), project('ops-1', 'ops'), project('server-guardian')],
    custom_dir: '/x',
  });
});
afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

describe('ArtifactsLink', () => {
  it('links a design debate to the artifacts of its thread', async () => {
    window.localStorage.setItem('guardian.project', 'arch-docs');
    await mount({ project: 'arch-docs' });
    const link = screen.getByRole('link', { name: ko.artifacts.openFromResult });
    expect(link.getAttribute('href')).toBe('#/debates/artifacts/thread_ab1');
  });

  it('treats a job without a project as the default project, which is a code project', async () => {
    window.localStorage.setItem('guardian.project', 'server-guardian');
    await mount({});
    expect(screen.queryByRole('link', { name: ko.artifacts.openFromResult })).toBeNull();
  });

  it('hides for code and ops projects', async () => {
    window.localStorage.setItem('guardian.project', 'ops-1');
    await mount({ project: 'ops-1' });
    expect(screen.queryByRole('link', { name: ko.artifacts.openFromResult })).toBeNull();
  });

  it('hides when the debate belongs to a design project other than the selected one', async () => {
    window.localStorage.setItem('guardian.project', 'arch-docs');
    await mount({ project: 'other-design' });
    expect(screen.queryByRole('link', { name: ko.artifacts.openFromResult })).toBeNull();
  });
});

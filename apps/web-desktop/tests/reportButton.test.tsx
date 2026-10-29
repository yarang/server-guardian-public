import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
import { GuardianRoot } from '@server-guardian/ui';
import * as api from '../src/api';
import { ko } from '../src/i18n/ko';
import { ReportButton } from '../src/pages/debate/ReportButton';
import { ProjectStoreProvider } from '../src/state/projectStore';
import type { ThreadReport } from '../src/types';

const mocked = vi.mocked(api);
const T = 'thread_ab12';
const svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>';
const report: ThreadReport = {
  thread_id: T,
  project: 'p',
  markdown: `# Result\n\n![Sum](${T}.assets/summary.svg)\n`,
  bytes: 10,
  updated_at: '2026-10-13T00:00:00Z',
  assets: [{ name: 'summary.svg', bytes: svg.length }],
};
const mount = () =>
  render(
    <GuardianRoot>
      <ReportButton thread={T} />
    </GuardianRoot>
  );

beforeEach(() => {
  mocked.fetchThreadReport.mockResolvedValue(report);
  mocked.fetchThreadReportAsset.mockResolvedValue({ name: 'summary.svg', bytes: svg.length, text: svg });
});
afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  invoke.mockReset();
});

describe('ReportButton', () => {
  it('fetches only when opened and renders the document', async () => {
    mount();
    expect(mocked.fetchThreadReport).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
    expect(await screen.findByRole('heading', { name: 'Result' })).toBeTruthy();
    expect(mocked.fetchThreadReport).toHaveBeenCalledWith(T);
    await waitFor(() => expect(document.querySelector('[data-asset="summary.svg"] svg')).toBeTruthy());
  });

  it('says there is no document yet on 404', async () => {
    mocked.fetchThreadReport.mockRejectedValue(new api.HubError('report not found', 404));
    mount();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
    expect(await screen.findByText(ko.debate.report.notFound)).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.debate.report.download }).hasAttribute('disabled')).toBe(true);
  });

  it('shows a retryable error on other failures', async () => {
    mocked.fetchThreadReport.mockRejectedValueOnce(new Error('timeout'));
    mount();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
    expect(await screen.findByText(new RegExp(ko.debate.report.loadFailed('timeout')))).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.retry }));
    expect(await screen.findByRole('heading', { name: 'Result' })).toBeTruthy();
  });

  it('downloads a self-contained <thread>.md through the desktop shell inside Tauri', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    invoke.mockResolvedValue('/Users/me/Downloads/thread_ab12.md');
    mount();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
    await screen.findByRole('heading', { name: 'Result' });
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.download }));
    expect(await screen.findByText(ko.debate.report.downloaded('/Users/me/Downloads/thread_ab12.md'))).toBeTruthy();
    expect(invoke).toHaveBeenCalledWith('save_download', { fileName: `${T}.md`, contents: expect.stringContaining('](data:image/svg+xml;base64,') });
    expect(invoke.mock.calls[0][1].contents).not.toContain('.assets/');
  });

  it('uses a Blob link outside Tauri', async () => {
    const create = vi.fn(() => 'blob:x');
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe(`${T}.md`);
    });
    mount();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
    await screen.findByRole('heading', { name: 'Result' });
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.download }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(create).toHaveBeenCalled();
  });

  it('reports a failed asset during download', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    mount();
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
    await screen.findByRole('heading', { name: 'Result' });
    mocked.fetchThreadReportAsset.mockRejectedValue(new Error('gone'));
    fireEvent.click(screen.getByRole('button', { name: ko.debate.report.download }));
    expect(await screen.findByText(ko.debate.report.downloadFailed('gone'))).toBeTruthy();
    expect(invoke).not.toHaveBeenCalled();
  });

  describe('keep as document', () => {
    const kept = { version: 1, sha256: 'a'.repeat(64), bytes: 10, warnings: [] as string[], file: { path: 'report.md' } as never };
    const open = async () => {
      mount();
      fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
      await screen.findByRole('heading', { name: 'Result' });
    };

    it('stores the report and links to the kept version', async () => {
      mocked.keepReport.mockResolvedValue(kept);
      await open();
      fireEvent.click(screen.getByRole('button', { name: ko.debate.report.keep }));
      expect(await screen.findByText(ko.debate.report.kept(1))).toBeTruthy();
      expect(mocked.keepReport).toHaveBeenCalledWith(T);
      expect(screen.getByRole('link', { name: ko.debate.report.openKept }).getAttribute('href')).toBe(`#/debates/artifacts/${T}`);
    });

    it('counts the warnings of a kept report', async () => {
      mocked.keepReport.mockResolvedValue({ ...kept, warnings: ['image target a.svg is not stored', 'link x is broken'] });
      await open();
      fireEvent.click(screen.getByRole('button', { name: ko.debate.report.keep }));
      expect(await screen.findByText(ko.debate.report.keptWarnings(2))).toBeTruthy();
      expect(screen.getByText('link x is broken')).toBeTruthy();
    });

    it('says so when the same text is already kept (409)', async () => {
      mocked.keepReport.mockRejectedValue(new api.HubError('v1 already holds this report', 409));
      await open();
      fireEvent.click(screen.getByRole('button', { name: ko.debate.report.keep }));
      expect(await screen.findByText(ko.debate.report.alreadyKept)).toBeTruthy();
      expect(screen.getByRole('link', { name: ko.debate.report.openKept })).toBeTruthy();
    });

    it('shows the validator message when the hub refuses the report', async () => {
      mocked.keepReport.mockRejectedValue(new api.HubError('raw HTML is not allowed', 400));
      await open();
      fireEvent.click(screen.getByRole('button', { name: ko.debate.report.keep }));
      expect(await screen.findByText(new RegExp(ko.debate.report.keepFailed('raw HTML is not allowed')))).toBeTruthy();
      expect(screen.queryByRole('link', { name: ko.debate.report.openKept })).toBeNull();
    });

    it.each([
      ['design', false],
      ['code', true],
    ])('with the project list loaded, a %s project shows the button: %s', async (kind, shown) => {
      mocked.fetchProjects.mockResolvedValue({ projects: [{ id: 'p', data: { kind } } as never], custom_dir: '/x' });
      mocked.fetchThreadReport.mockResolvedValue({ ...report, project: 'p' });
      render(
        <GuardianRoot>
          <ProjectStoreProvider>
            <ReportButton thread={T} />
          </ProjectStoreProvider>
        </GuardianRoot>
      );
      fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
      await screen.findByRole('heading', { name: 'Result' });
      await waitFor(() => expect(mocked.fetchProjects).toHaveBeenCalled());
      await waitFor(() => expect(screen.queryByRole('button', { name: ko.debate.report.keep }) !== null).toBe(shown));
      await new Promise((r) => setTimeout(r, 20));
      expect(screen.queryByRole('button', { name: ko.debate.report.keep }) !== null).toBe(shown);
    });

    it('is disabled while there is no report', async () => {
      mocked.fetchThreadReport.mockRejectedValue(new api.HubError('report not found', 404));
      mount();
      fireEvent.click(screen.getByRole('button', { name: ko.debate.report.open }));
      await screen.findByText(ko.debate.report.notFound);
      expect(screen.getByRole('button', { name: ko.debate.report.keep }).hasAttribute('disabled')).toBe(true);
    });
  });
});

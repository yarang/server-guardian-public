import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn().mockResolvedValue({ svg: '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>' }) },
}));
import { GuardianRoot } from '@server-guardian/ui';
import * as api from '../src/api';
import { ko } from '../src/i18n/ko';
import { ReportView } from '../src/pages/debate/ReportButton';
import type { ThreadReport } from '../src/types';

const T = 'thread_ab12';
const report: ThreadReport = {
  thread_id: T,
  project: 'p',
  markdown: '# Result\n\n```mermaid\nflowchart LR\n  A --> B\n```\n',
  bytes: 10,
  updated_at: '2026-10-28T00:00:00Z',
  assets: [],
};

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  invoke.mockReset();
});

describe('saving a diagram of a report', () => {
  it('writes <thread>-diagram-<n>.mmd with the diagram source through the desktop shell', async () => {
    vi.mocked(api).fetchThreadReport.mockResolvedValue(report);
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    invoke.mockResolvedValue('/Users/me/Downloads/thread_ab12-diagram-1.mmd');
    render(
      <GuardianRoot>
        <ReportView thread={T} />
      </GuardianRoot>
    );
    fireEvent.click(await screen.findByRole('button', { name: ko.debate.report.saveDiagram }));
    expect(await screen.findByText(ko.debate.report.downloaded('/Users/me/Downloads/thread_ab12-diagram-1.mmd'))).toBeTruthy();
    expect(invoke).toHaveBeenCalledWith('save_download', { fileName: `${T}-diagram-1.mmd`, contents: 'flowchart LR\n  A --> B\n' });
  });

  it('says so when the save fails', async () => {
    vi.mocked(api).fetchThreadReport.mockResolvedValue(report);
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    invoke.mockRejectedValue(new Error('disk full'));
    render(
      <GuardianRoot>
        <ReportView thread={T} />
      </GuardianRoot>
    );
    fireEvent.click(await screen.findByRole('button', { name: ko.debate.report.saveDiagram }));
    expect(await screen.findByText(ko.debate.report.downloadFailed('disk full'))).toBeTruthy();
  });
});

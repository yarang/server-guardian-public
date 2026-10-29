import { act, fireEvent, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { ArtifactManifest, ArtifactProjectList, ArtifactVersionList, ProjectEntry } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { DebatesPage } from '../src/pages/DebatesPage';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';

const mocked = vi.mocked(api);

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

function Routed() {
  const { params } = useRoute();
  return <DebatesPage jobId={params[0]} wikiPage={params[1]} extra={params.slice(2)} />;
}

async function mount(hash: string) {
  window.location.hash = hash;
  render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle(300);
}

const project = (id: string, kind?: 'design' | 'ops'): ProjectEntry =>
  ({ id, source: 'custom', data: { id, name: id, kind }, errors: [], status: { present: true } }) as ProjectEntry;

const sha = 'a'.repeat(64);
const file = (path: string, kind: string, extra: object = {}) => ({
  path, sha256: sha, bytes: 120, mime: kind === 'svg' ? 'image/svg+xml' : 'text/markdown', kind, stage: 'draft', agent: 'expert', title: path, ...extra,
});

const list: ArtifactProjectList = {
  project: 'arch-docs',
  threads: [{ thread_id: 'thread_ab1', versions: 2, latest: { version: 2, created_at: '2026-10-18T09:00:00Z', written: 'a', files: 3, bytes: 360 } }],
};
const versions: ArtifactVersionList = {
  thread_id: 'thread_ab1', project: 'arch-docs',
  versions: [
    { version: 1, created_at: '2026-10-17T09:00:00Z', written: 'a', files: 1, bytes: 120, expired_at: '2026-10-18T00:00:00Z' },
    { version: 2, created_at: '2026-10-18T09:00:00Z', written: 'a', files: 3, bytes: 360 },
  ],
};
const manifest: ArtifactManifest = {
  manifest_version: 1, version: 2, project: 'arch-docs', thread: 'thread_ab1', created_at: '2026-10-18T09:00:00Z', parent: 1, written: 'a',
  files: [file('design.md', 'markdown'), file('flow.mmd', 'mermaid'), file('logo.svg', 'svg')],
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-18T10:00:00Z'));
  window.localStorage.setItem('guardian.project', 'arch-docs');
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.fetchProjects.mockResolvedValue({ projects: [project('arch-docs', 'design'), project('server-guardian')], custom_dir: '/x' });
  mocked.fetchProjectArtifacts.mockResolvedValue(list);
  mocked.fetchArtifactVersions.mockResolvedValue(versions);
  mocked.fetchArtifactManifest.mockResolvedValue(manifest);
  mocked.fetchArtifactText.mockImplementation(async (_t, _v, name) =>
    name === 'logo.svg'
      ? '<svg xmlns="http://www.w3.org/2000/svg"><script>window.pwned=1</script><circle r="4"/></svg>'
      : '# Design\n\nBody text.'
  );
});
afterEach(() => {
  window.localStorage.clear();
  vi.useRealTimers();
});

describe('Artifacts tab', () => {
  it('is offered for a design project and lists the threads that have artifacts', async () => {
    await mount('#/debates/artifacts');
    expect(screen.getByRole('button', { name: ko.debate.page.tabArtifacts })).toBeTruthy();
    expect(mocked.fetchProjectArtifacts).toHaveBeenCalledWith('arch-docs');
    const link = screen.getByRole('link', { name: ko.artifacts.openThread('thread_ab1') });
    expect(link.getAttribute('href')).toBe('#/debates/artifacts/thread_ab1');
  });

  it('is not offered for a code project', async () => {
    window.localStorage.setItem('guardian.project', 'server-guardian');
    await mount('#/debates');
    expect(screen.queryByRole('button', { name: ko.debate.page.tabArtifacts })).toBeNull();
    expect(mocked.fetchProjectArtifacts).not.toHaveBeenCalled();
  });

  it('opens for a code project by link, so a kept report can be converted', async () => {
    window.localStorage.setItem('guardian.project', 'server-guardian');
    await mount('#/debates/artifacts/thread_ab1');
    expect(screen.queryByRole('button', { name: ko.debate.page.tabArtifacts })).toBeNull();
    expect(mocked.fetchArtifactManifest).toHaveBeenCalledWith('thread_ab1', 2);
    expect(screen.getByRole('link', { name: ko.artifacts.openFile('design.md') })).toBeTruthy();
  });

  it('shows the latest version files with their kind and marks tombstoned versions', async () => {
    await mount('#/debates/artifacts/thread_ab1');
    expect(mocked.fetchArtifactManifest).toHaveBeenCalledWith('thread_ab1', 2);
    expect(screen.getByRole('link', { name: ko.artifacts.openFile('design.md') })).toBeTruthy();
    expect(screen.getByText(ko.artifacts.kindLabel.mermaid)).toBeTruthy();
    expect(screen.getByRole('link', { name: ko.artifacts.openVersion(1) })).toBeTruthy();
  });

  it('shows an SVG only as an image, never as inline markup', async () => {
    await mount('#/debates/artifacts/thread_ab1/v2/logo.svg');
    const img = screen.getByAltText(ko.artifacts.svgAlt('logo.svg')) as HTMLImageElement;
    expect(img.src.startsWith('data:image/svg+xml;base64,')).toBe(true);
    expect(document.querySelector('svg circle')).toBeNull();
    expect(document.querySelector('script')).toBeNull();
    expect((window as unknown as { pwned?: number }).pwned).toBeUndefined();
  });

  it('renders a Markdown file through MarkdownDoc', async () => {
    await mount('#/debates/artifacts/thread_ab1/v2/design.md');
    expect(screen.getByTestId('md-doc').textContent).toContain('Body text.');
  });

  it('tells that the stored content changed when the hub answers 409', async () => {
    mocked.fetchArtifactText.mockRejectedValue(new api.HubError('artifact content changed', 409));
    await mount('#/debates/artifacts/thread_ab1/v2/design.md');
    expect(screen.getByText(ko.artifacts.changed)).toBeTruthy();
  });

  it('offers "Make Word document" only on a Markdown file without a DOCX beside it', async () => {
    mocked.fetchArtifactManifest.mockResolvedValue({
      ...manifest,
      files: [...manifest.files, file('done.md', 'markdown'), file('done.docx', 'docx', { source: 'done.md', source_sha256: sha })],
    });
    await mount('#/debates/artifacts/thread_ab1');
    // design.md has no docx yet, done.md has one; the diagram and the image never get the button.
    expect(screen.getAllByRole('button', { name: ko.artifacts.office.docx.make })).toHaveLength(1);
    expect(screen.getByRole('button', { name: ko.artifacts.office.docx.make }).getAttribute('title')).toBe(ko.artifacts.office.docx.makeTitle('design.md'));
    expect(screen.getByText(ko.artifacts.kindLabel.docx)).toBeTruthy();
    // each format has its own button: done.md still lacks a PowerPoint file, design.md lacks both.
    expect(screen.getAllByRole('button', { name: ko.artifacts.office.pptx.make })).toHaveLength(2);
  });

  it('asks for a PowerPoint file with its format and never shows it as text', async () => {
    const converted = { ...manifest, files: [...manifest.files, file('design.pptx', 'pptx', { source: 'design.md', source_sha256: sha })] };
    mocked.fetchArtifactManifest.mockResolvedValueOnce(manifest).mockResolvedValue(converted);
    mocked.convertArtifact.mockResolvedValue({ source: 'design.md', source_sha256: sha, result_sha256: 'b'.repeat(64), file: converted.files[3] });
    await mount('#/debates/artifacts/thread_ab1');
    fireEvent.click(screen.getByRole('button', { name: ko.artifacts.office.pptx.make }));
    await settle(300);
    expect(mocked.convertArtifact).toHaveBeenCalledWith('thread_ab1', 2, 'design.md', 'pptx');
    expect(screen.getByText(ko.artifacts.kindLabel.pptx)).toBeTruthy();
    expect(screen.queryByRole('button', { name: ko.artifacts.office.pptx.make })).toBeNull();
    expect(screen.getByRole('button', { name: ko.artifacts.office.docx.make })).toBeTruthy();
  });

  it('shows a PowerPoint file as a derived copy to download, not as text', async () => {
    mocked.fetchArtifactManifest.mockResolvedValue({
      ...manifest,
      files: [...manifest.files, file('design.pptx', 'pptx', { source: 'design.md', source_sha256: sha })],
    });
    await mount('#/debates/artifacts/thread_ab1/v2/design.pptx');
    expect(screen.getByText(ko.artifacts.office.pptx.derivedFrom('design.md'))).toBeTruthy();
    expect(mocked.fetchArtifactText).not.toHaveBeenCalled();
  });

  it('converts into the same version, then reads the listing again so the new file shows up', async () => {
    const converted = { ...manifest, files: [...manifest.files, file('design.docx', 'docx', { source: 'design.md', source_sha256: sha })] };
    mocked.fetchArtifactManifest.mockResolvedValueOnce(manifest).mockResolvedValue(converted);
    mocked.convertArtifact.mockResolvedValue({ source: 'design.md', source_sha256: sha, result_sha256: 'b'.repeat(64), file: converted.files[3] });
    await mount('#/debates/artifacts/thread_ab1');
    fireEvent.click(screen.getByRole('button', { name: ko.artifacts.office.docx.make }));
    await settle(300);
    expect(mocked.convertArtifact).toHaveBeenCalledWith('thread_ab1', 2, 'design.md', 'docx');
    expect(mocked.fetchArtifactManifest).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('link', { name: ko.artifacts.openFile('design.docx') })).toBeTruthy();
    expect(screen.queryByRole('button', { name: ko.artifacts.office.docx.make })).toBeNull();
  });

  it('shows the hub reason when the conversion is refused and keeps the button', async () => {
    mocked.convertArtifact.mockRejectedValue(new api.HubError('markdown too large', 400));
    await mount('#/debates/artifacts/thread_ab1');
    fireEvent.click(screen.getByRole('button', { name: ko.artifacts.office.docx.make }));
    await settle(300);
    expect(screen.getByText(ko.artifacts.office.docx.failed('markdown too large'))).toBeTruthy();
    expect(screen.getByRole('button', { name: ko.artifacts.office.docx.make })).toBeTruthy();
  });

  it('never fetches a DOCX as text or renders it; its download goes through the binary call', async () => {
    mocked.fetchArtifactManifest.mockResolvedValue({
      ...manifest,
      files: [...manifest.files, file('design.docx', 'docx', { source: 'design.md', source_sha256: sha, mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })],
    });
    mocked.fetchArtifactBlob.mockResolvedValue(new Blob(['PK']));
    const created = vi.fn(() => 'blob:x');
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }));
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    await mount('#/debates/artifacts/thread_ab1/v2/design.docx');
    expect(screen.getByText(ko.artifacts.office.docx.derivedFrom('design.md'))).toBeTruthy();
    expect(screen.queryByTestId('md-doc')).toBeNull();
    expect(mocked.fetchArtifactText).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: ko.artifacts.download }));
    await settle(0);
    expect(mocked.fetchArtifactBlob).toHaveBeenCalledWith('thread_ab1', 2, 'design.docx');
    expect(created).toHaveBeenCalled();
    expect(clicked).toHaveBeenCalledTimes(1);
    clicked.mockRestore();
    expect(screen.getByText(ko.artifacts.downloaded('design.docx'))).toBeTruthy();
  });
});

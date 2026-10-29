import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ko } from '../src/i18n/ko';
import type { CodeFileDetail, CodeFileLines, CodeFileList } from '../src/types';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { fileHref, parseLineRange } from '../src/lib/wiki';
import { DebatesPage } from '../src/pages/DebatesPage';
import { filterPaths } from '../src/pages/wiki/FileList';
import { findDecl, rangeAround } from '../src/pages/wiki/FileView';
import { useRoute } from '../src/router';
import { AppStores } from '../src/state/AppStores';
import { follow } from './nav';

const mocked = vi.mocked(api);

const settle = (ms = 0) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

function Routed() {
  const { params } = useRoute();
  return <DebatesPage jobId={params[0]} wikiPage={params[1]} />;
}

async function mount(hash: string) {
  window.location.hash = hash;
  const view = render(
    <GuardianRoot>
      <AppStores>
        <Routed />
      </AppStores>
    </GuardianRoot>
  );
  await settle(300);
  return view;
}

const SOURCE = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`);

const files: CodeFileList = {
  project: 'server-guardian',
  sha: 'abcdef1234567',
  truncated: false,
  files: [
    { path: 'cli/llm-agent/wiki_build.py', size: 10 },
    { path: 'cli/llm-agent/code_wiki.py', size: 10 },
    { path: 'docs/design/wiki.md', size: 10 },
  ],
};

const detail: CodeFileDetail = {
  project: 'server-guardian',
  sha: 'abcdef1234567',
  path: 'cli/llm-agent/wiki_build.py',
  language: 'python',
  size: 2048,
  binary: false,
  lines: 30,
  indexed: true,
  outline: [
    { name: 'WikiBuilder', qualified: 'WikiBuilder', kind: 'class', start: 10, end: 20, signature: 'class WikiBuilder:' },
    { name: 'run', qualified: 'WikiBuilder.run', kind: 'method', start: 12, end: 14, signature: 'def run(self):' },
  ],
  incoming: [{ path: 'cli/llm-agent/guardian.py', count: 5, test: false }],
  outgoing: [{ path: 'cli/llm-agent/code_wiki.py', count: 56, test: false }],
  references: 'name based',
  pages: [{ id: 'cli-llm-agent', title: 'CLI', state: 'stale', via: 'scope' }],
  docs: [{ path: 'docs/design/wiki.md', title: 'Wiki design', lines: [3, 9] }],
};

function linesOf(start: number, end: number): CodeFileLines {
  const last = Math.min(end, SOURCE.length);
  return {
    project: 'server-guardian', sha: 'abcdef1234567', path: detail.path, start, end: last, total: SOURCE.length,
    lines: SOURCE.slice(start - 1, last), truncated: false,
  };
}

beforeEach(() => {
  for (const fn of [mocked.fetchCodeFiles, mocked.fetchCodeFile, mocked.fetchCodeFileLines, mocked.fetchDebates]) fn.mockReset();
  vi.useFakeTimers();
  mocked.fetchDebates.mockResolvedValue([]);
  mocked.fetchCodeFiles.mockResolvedValue(files);
  mocked.fetchCodeFile.mockResolvedValue(detail);
  mocked.fetchCodeFileLines.mockImplementation(async (_p, _path, start, end) => linesOf(start, end));
});
afterEach(() => {
  window.location.hash = '';
  vi.useRealTimers();
});

describe('file view helpers', () => {
  it('builds and parses file locations', () => {
    expect(fileHref('a/b.py', { lines: '3-4' })).toBe('#/debates/wiki?file=a%2Fb.py&lines=3-4');
    expect(fileHref('a/b.py', { symbol: 'X.y' })).toBe('#/debates/wiki?file=a%2Fb.py&symbol=X.y');
    expect(parseLineRange('7')).toEqual({ start: 7, end: 7 });
    expect(parseLineRange('7-9')).toEqual({ start: 7, end: 9 });
    expect(parseLineRange('9-7')).toBeNull();
    expect(parseLineRange('0')).toBeNull();
    expect(parseLineRange('x')).toBeNull();
  });

  it('opens a window with context around a range, within the file and the hub limit', () => {
    expect(rangeAround(20, 22, 1000)).toEqual({ start: 12, end: 131, highlight: { start: 20, end: 22 } });
    expect(rangeAround(3, 3, 10)).toEqual({ start: 1, end: 10, highlight: { start: 3, end: 3 } });
    const long = rangeAround(100, 900, 2000);
    expect(long.end - long.start + 1).toBe(400);
  });

  it('finds a declaration by qualified name, suffix or name', () => {
    expect(findDecl(detail.outline, 'WikiBuilder.run')?.start).toBe(12);
    expect(findDecl(detail.outline, 'run')?.start).toBe(12);
    expect(findDecl(detail.outline, 'WikiBuilder')?.start).toBe(10);
    expect(findDecl(detail.outline, 'nope')).toBeUndefined();
  });

  it('filters paths by every term', () => {
    const paths = files.files.map((f) => f.path);
    expect(filterPaths(paths, 'wiki PY')).toEqual(['cli/llm-agent/wiki_build.py', 'cli/llm-agent/code_wiki.py']);
    expect(filterPaths(paths, '  ')).toEqual(paths);
  });
});

describe('Wiki view: files', () => {
  it('switches to the file list, searches and opens a file', async () => {
    await mount('#/debates/wiki');
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.modes.files }));
    await settle(300);
    expect(window.location.hash).toBe('#/debates/wiki?view=files');
    expect(mocked.fetchCodeFiles).toHaveBeenCalledWith('server-guardian');
    expect(screen.getByText(ko.wiki.files.selectFile)).toBeTruthy();

    fireEvent.change(screen.getByLabelText(ko.wiki.files.search), { target: { value: 'build' } });
    const nav = screen.getByRole('navigation', { name: ko.wiki.files.listAria });
    const rows = within(nav).getAllByRole('link');
    expect(rows.map((r) => r.getAttribute('title'))).toEqual(['cli/llm-agent/wiki_build.py']);
    follow(rows[0]);
    await settle(300);
    expect(mocked.fetchCodeFile).toHaveBeenCalledWith('server-guardian', 'cli/llm-agent/wiki_build.py');
    expect(screen.getByText(ko.wiki.files.meta('python', 30, '2.0'))).toBeTruthy();
    // No source until something asks for it.
    expect(mocked.fetchCodeFileLines).not.toHaveBeenCalled();
    expect(screen.getByText(ko.wiki.files.sourceHint)).toBeTruthy();
  });

  it('shows pages, documents, references and a declaration on click', async () => {
    await mount(`#/debates/wiki?file=${encodeURIComponent(detail.path)}`);
    expect(screen.getByRole('link', { name: /CLI/ }).getAttribute('href')).toBe('#/debates/wiki/cli-llm-agent');
    expect(screen.getByText(`(${ko.wiki.files.via.scope})`)).toBeTruthy();
    expect(screen.getByRole('link', { name: ko.wiki.files.docLine(9) }).getAttribute('href')).toBe(fileHref('docs/design/wiki.md', { lines: '9' }));
    expect(screen.getByRole('link', { name: 'cli/llm-agent/code_wiki.py' }).getAttribute('href')).toBe(fileHref('cli/llm-agent/code_wiki.py'));
    expect(screen.getByText(ko.wiki.files.incomingTitle(1))).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: ko.wiki.files.showDecl('WikiBuilder.run') }));
    await settle(10);
    expect(mocked.fetchCodeFileLines).toHaveBeenLastCalledWith('server-guardian', detail.path, 4, 30);
    const source = screen.getByTestId('file-source');
    const marked = Array.from(source.querySelectorAll('[data-marked]')).map((el) => el.getAttribute('data-line'));
    expect(marked).toEqual(['12', '13', '14']);
    expect(screen.getByText(ko.wiki.files.sourceRange(4, 30, 30))).toBeTruthy();
  });

  it('opens at cited lines, and at a cited symbol', async () => {
    await mount(`#/debates/wiki?file=${encodeURIComponent(detail.path)}&lines=20-21`);
    expect(mocked.fetchCodeFileLines).toHaveBeenLastCalledWith('server-guardian', detail.path, 12, 30);
    expect(screen.getByText(ko.wiki.files.lineShift)).toBeTruthy();
    let marked = Array.from(screen.getByTestId('file-source').querySelectorAll('[data-marked]')).map((el) => el.textContent);
    expect(marked).toEqual(['20line 20', '21line 21']);

    window.location.hash = `#/debates/wiki?file=${encodeURIComponent(detail.path)}&symbol=WikiBuilder`;
    await settle(300);
    marked = Array.from(screen.getByTestId('file-source').querySelectorAll('[data-marked]')).map((el) => el.getAttribute('data-line'));
    expect(marked[0]).toBe('10');
    expect(marked).toHaveLength(11);
  });

  it('says when a cited symbol is gone, and when the file is not text', async () => {
    await mount(`#/debates/wiki?file=${encodeURIComponent(detail.path)}&symbol=Gone`);
    expect(screen.getByText(ko.wiki.files.symbolMissing('Gone'))).toBeTruthy();
    expect(mocked.fetchCodeFileLines).not.toHaveBeenCalled();

    mocked.fetchCodeFile.mockResolvedValue({ ...detail, path: 'img.png', binary: true, outline: [] });
    window.location.hash = `#/debates/wiki?file=img.png`;
    await settle(300);
    expect(screen.getByText(ko.wiki.files.binary)).toBeTruthy();
  });

  it('pages through the source', async () => {
    await mount(`#/debates/wiki?file=${encodeURIComponent(detail.path)}`);
    fireEvent.click(screen.getByRole('button', { name: ko.wiki.files.showTop }));
    await settle(10);
    expect(mocked.fetchCodeFileLines).toHaveBeenLastCalledWith('server-guardian', detail.path, 1, 30);
    expect((screen.getByRole('button', { name: ko.wiki.files.next }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: ko.wiki.files.prev }) as HTMLButtonElement).disabled).toBe(true);
  });
});

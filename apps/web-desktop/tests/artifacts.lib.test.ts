import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError, fetchArtifactManifest, fetchArtifactText, fetchArtifactVersions, fetchProjectArtifacts } from '../src/api';
import { artifactParams, canMakeDerived, derivedFormat, derivedNameFor, mermaidAsMarkdown, parseArtifactRoute, svgDataUrl } from '../src/lib/artifacts';

afterEach(() => vi.unstubAllGlobals());

describe('artifact helpers', () => {
  it('wraps a mermaid source in a fence the content cannot close', () => {
    expect(mermaidAsMarkdown('graph TD\n  A-->B\n\n')).toBe('```mermaid\ngraph TD\n  A-->B\n```\n');
    const tricky = 'graph TD\n  A["```"]-->B';
    const out = mermaidAsMarkdown(tricky);
    expect(out.startsWith('````mermaid\n')).toBe(true);
    expect(out.trimEnd().endsWith('````')).toBe(true);
  });

  it('turns SVG text into a base64 data URL that decodes back, unicode included', () => {
    const svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>한글 ✓</text><script>alert(1)</script></svg>';
    const url = svgDataUrl(svg);
    expect(url.startsWith('data:image/svg+xml;base64,')).toBe(true);
    const bytes = Uint8Array.from(atob(url.split(',')[1]), (c) => c.charCodeAt(0));
    expect(new TextDecoder().decode(bytes)).toBe(svg);
  });

  it('parses and builds artifact route params', () => {
    expect(parseArtifactRoute([])).toEqual({});
    expect(parseArtifactRoute(['t1'])).toEqual({ thread: 't1' });
    expect(parseArtifactRoute(['t1', 'v3'])).toEqual({ thread: 't1', version: 3 });
    expect(parseArtifactRoute(['t1', 'v3', 'a.md'])).toEqual({ thread: 't1', version: 3, file: 'a.md' });
    // A malformed version is ignored rather than guessed at.
    expect(parseArtifactRoute(['t1', 'v0', 'a.md'])).toEqual({ thread: 't1' });
    expect(parseArtifactRoute(['t1', 'latest'])).toEqual({ thread: 't1' });
    expect(artifactParams({})).toEqual(['artifacts']);
    expect(artifactParams({ thread: 't1', version: 2, file: 'a.md' })).toEqual(['artifacts', 't1', 'v2', 'a.md']);
  });
});

describe('artifact API', () => {
  const lastUrl = (m: ReturnType<typeof vi.fn>) => String(m.mock.calls[m.mock.calls.length - 1][0]);
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

  it('encodes the path pieces and never puts a token in the URL', async () => {
    const m = vi.fn().mockImplementation(async () => json({ project: 'p', threads: [], versions: [], files: [] }));
    vi.stubGlobal('fetch', m);
    await fetchProjectArtifacts('my proj');
    expect(lastUrl(m)).toContain('/api/projects/my%20proj/artifacts');
    await fetchArtifactVersions('t 1');
    expect(lastUrl(m)).toContain('/api/threads/t%201/artifacts');
    await fetchArtifactManifest('t1', 2);
    expect(lastUrl(m)).toContain('/api/threads/t1/artifacts/v2');
    m.mockImplementation(async () => new Response('# hi', { status: 200 }));
    expect(await fetchArtifactText('t1', 2, 'design.md')).toBe('# hi');
    expect(lastUrl(m)).toContain('/api/threads/t1/artifacts/v2/files/design.md');
    expect(m.mock.calls.every((c) => !String(c[0]).includes('access_token'))).toBe(true);
  });

  it('reports the hub error and status of a failed file read', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => json({ error: 'artifact content changed' }, 409)));
    const err = await fetchArtifactText('t1', 1, 'a.md').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(HubError);
    expect(err).toMatchObject({ status: 409, message: 'artifact content changed' });
  });
});

describe('artifactViewForName', () => {
  it('follows the hub extension rule, ignoring case', async () => {
    const { artifactViewForName } = await import('../src/lib/artifacts');
    expect(artifactViewForName('ui-review.md')).toBe('markdown');
    expect(artifactViewForName('Flow.MMD')).toBe('mermaid');
    expect(artifactViewForName('logo.svg')).toBe('svg');
    expect(artifactViewForName('notes.txt')).toBeNull();
    expect(artifactViewForName('md')).toBeNull();
    expect(artifactViewForName(undefined)).toBeNull();
  });
});

describe('derived Office files', () => {
  const f = (path: string, kind: string, extra: object = {}) => ({ path, kind, sha256: 'a', bytes: 1, mime: '', stage: '', agent: '', title: '', ...extra });

  it('names the derived file after its Markdown source', () => {
    expect(derivedNameFor('overview.md', 'docx')).toBe('overview.docx');
    expect(derivedNameFor('a.b.md', 'pptx')).toBe('a.b.pptx');
  });

  it('knows which kinds are derived Office files', () => {
    expect(['docx', 'pptx', 'markdown', 'svg'].map((kind) => derivedFormat(f('x', kind)))).toEqual(['docx', 'pptx', null, null]);
  });

  it('offers the conversion only for live Markdown without a sibling', () => {
    const files = [f('a.md', 'markdown'), f('b.md', 'markdown'), f('b.docx', 'docx'), f('c.mmd', 'mermaid'), f('d.md', 'markdown', { expired: true })];
    expect(files.map((x) => canMakeDerived(x, files, 'docx'))).toEqual([true, false, false, false, false]);
    expect(files.map((x) => canMakeDerived(x, files, 'pptx'))).toEqual([true, true, false, false, false]);
  });
});

import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError, deleteWikiPage, fetchWikiPage, fetchWikiPages, saveWikiPage, startWikiBuild } from '../src/api';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => vi.unstubAllGlobals());

function lastCall(fetchMock: ReturnType<typeof vi.fn>) {
  const [url, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
  return { url: String(url), method: String(init?.method ?? 'GET'), body: init?.body as string | undefined };
}

describe('code wiki API', () => {
  it('lists and reads pages with encoded ids and an optional sha', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => json({ project: 'p', sha: 'abc', pages: [], errors: [] }));
    vi.stubGlobal('fetch', fetchMock);
    await fetchWikiPages('my proj');
    expect(lastCall(fetchMock).url).toContain('/api/projects/my%20proj/wiki');
    expect(lastCall(fetchMock).url).not.toContain('sha=');
    await fetchWikiPages('p', 'abc123');
    expect(lastCall(fetchMock).url).toContain('/api/projects/p/wiki?sha=abc123');
    await fetchWikiPage('p', 'cli-llm-agent', 'abc123');
    expect(lastCall(fetchMock).url).toContain('/api/projects/p/wiki/cli-llm-agent?sha=abc123');
  });

  it('saves with PUT and deletes with DELETE', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => json({ ok: true, deleted: 'x' }));
    vi.stubGlobal('fetch', fetchMock);
    await saveWikiPage('p', 'x', { title: 'T', scope: ['a/'], body: 'b' });
    expect(lastCall(fetchMock)).toMatchObject({ method: 'PUT', body: JSON.stringify({ title: 'T', scope: ['a/'], body: 'b' }) });
    expect(lastCall(fetchMock).url).toContain('/api/projects/p/wiki/x');
    await deleteWikiPage('p', 'x');
    expect(lastCall(fetchMock).method).toBe('DELETE');
  });

  it('starts a build with POST and surfaces 409 and 429 messages', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(json({ id: 'dbt_1', kind: 'code_wiki', status: 'running' }, 202));
    vi.stubGlobal('fetch', fetchMock);
    const job = await startWikiBuild('p', { stale: true });
    expect(job.kind).toBe('code_wiki');
    expect(lastCall(fetchMock)).toMatchObject({ method: 'POST', body: '{"stale":true}' });
    expect(lastCall(fetchMock).url).toContain('/api/projects/p/wiki/build');

    fetchMock.mockResolvedValueOnce(json({ error: 'a code wiki build is already running for this project' }, 409));
    await expect(startWikiBuild('p', {})).rejects.toMatchObject({ status: 409, message: expect.stringContaining('already running') });
    fetchMock.mockResolvedValueOnce(json({ error: 'too many running code wiki builds (max 1)' }, 429));
    await expect(startWikiBuild('q', {})).rejects.toBeInstanceOf(HubError);
  });
});

import { afterEach, describe, expect, it, vi } from 'vitest';
import { HubError, pollDebateEvents, streamDebateEvents } from '../src/api';
import type { DebateStreamMessage } from '../src/types';

function ndjson(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      chunks.forEach((x) => c.enqueue(encoder.encode(x)));
      c.close();
    },
  });
  return new Response(body, { status, headers: { 'Content-Type': 'application/x-ndjson' } });
}
const line = (o: unknown) => `${JSON.stringify(o)}\n`;
const e = (seq: number, type = 'round_started') => ({ seq, ts: '2026-10-08T05:21:00Z', type, round: 1 });

afterEach(() => vi.unstubAllGlobals());

describe('pollDebateEvents', () => {
  it('requests after=N and keeps only well-formed events', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ events: [e(5), { nope: 1 }, e(6)], next: 6, done: true }), { status: 200 })
    );
    vi.stubGlobal('fetch', fetchMock);
    const page = await pollDebateEvents('job 1', 4);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/debates/job%201/events?after=4');
    expect(page.events.map((x) => x.seq)).toEqual([5, 6]);
    expect(page).toMatchObject({ next: 6, done: true });
  });

  it('treats null events as empty and keeps the cursor', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ events: null, done: false }), { status: 200 })));
    expect(await pollDebateEvents('j', 9)).toEqual({ events: [], next: 9, done: false });
  });
});

describe('streamDebateEvents', () => {
  it('parses lines split across chunks and stops at end', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ndjson([line(e(1)).slice(0, 12), line(e(1)).slice(12) + line({ type: 'heartbeat' }) + line(e(2)).slice(0, 5), line(e(2)).slice(5) + 'not json\n' + line({ type: 'end', status: 'completed' })])
    );
    vi.stubGlobal('fetch', fetchMock);
    const got: DebateStreamMessage[] = [];
    const res = await streamDebateEvents('j', 0, (m) => got.push(m), new AbortController().signal);
    expect(got.map((m) => m.type)).toEqual(['round_started', 'heartbeat', 'round_started', 'end']);
    expect(res).toEqual({ ended: true, status: 'completed', lastSeq: 2 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/events?after=0&follow=1');
  });

  it('reconnects after a drop with after=<last seq> and skips replayed events', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ndjson([line(e(1)), line(e(2))])) // closes without end
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(ndjson([line(e(2)), line(e(3)), line({ type: 'end', status: 'failed' })]));
    vi.stubGlobal('fetch', fetchMock);
    const states: string[] = [];
    const seqs: number[] = [];
    const res = await streamDebateEvents('j', 0, (m) => 'seq' in m && seqs.push(m.seq), new AbortController().signal, {
      retryBaseMs: 1, retryMaxMs: 2, onConnection: (s) => states.push(s),
    });
    expect(seqs).toEqual([1, 2, 3]);
    expect(res).toMatchObject({ ended: true, status: 'failed', lastSeq: 3 });
    expect(fetchMock.mock.calls.map((c) => String(c[0]).match(/after=(\d+)/)?.[1])).toEqual(['0', '2', '2']);
    expect(states).toEqual(['live', 'reconnecting', 'reconnecting', 'live']);
  });

  it('rejects on a 4xx answer instead of retrying', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'debate not found' }), { status: 404 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(streamDebateEvents('j', 0, () => undefined, new AbortController().signal)).rejects.toBeInstanceOf(HubError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('resolves unended when aborted while waiting to reconnect', async () => {
    const controller = new AbortController();
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => {
      queueMicrotask(() => controller.abort());
      return ndjson([line(e(1))]);
    }));
    const res = await streamDebateEvents('j', 0, () => undefined, controller.signal, { retryBaseMs: 60000 });
    expect(res).toMatchObject({ ended: false, lastSeq: 1 });
  });
});

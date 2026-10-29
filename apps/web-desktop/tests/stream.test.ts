import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseSSE, streamFleetExec } from '../src/api';
import { ko } from '../src/i18n/ko';
import type { FleetStreamEvent } from '../src/types';

function sseResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((c) => controller.enqueue(encoder.encode(c)));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { 'Content-Type': 'text/event-stream' } });
}

function run(command: string, targets: string[] = []) {
  const events: FleetStreamEvent[] = [];
  return new Promise<{ events: FleetStreamEvent[]; error?: Error }>((resolve) => {
    streamFleetExec(
      command,
      targets,
      (e) => {
        events.push(e);
        if (e.type === 'complete' || e.type === 'error') resolve({ events });
      },
      (error) => resolve({ events, error })
    );
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('parseSSE', () => {
  it('returns complete events and keeps the unfinished tail', () => {
    const { events, rest } = parseSSE('event: start\ndata: {"a":1}\n\nevent: node_done\r\ndata: {"b"');
    expect(events).toEqual([{ event: 'start', data: '{"a":1}' }]);
    expect(rest).toBe('event: node_done\ndata: {"b"');
  });

  it('joins multi-line data and defaults the event name', () => {
    expect(parseSSE('data: one\ndata: two\n\n').events).toEqual([{ event: 'message', data: 'one\ntwo' }]);
  });
});

describe('streamFleetExec', () => {
  it('POSTs the command in a JSON body and parses events split across chunks', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        'event: start\ndata: {"type":"start","total":2}\n\nevent: node_do',
        'ne\ndata: {"type":"node_done","node_id":"a"}\n\n',
        'event: complete\ndata: {"type":"complete"}\n\n',
      ])
    );
    vi.stubGlobal('fetch', fetchMock);

    const { events, error } = await run('uptime; id', ['a', 'b']);

    expect(error).toBeUndefined();
    expect(events.map((e) => e.type)).toEqual(['start', 'node_done', 'complete']);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/api\/fleet\/exec\/stream$/);
    expect(url).not.toContain('uptime');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ command: 'uptime; id', targets: ['a', 'b'], timeout_sec: 15 });
    expect(new Headers(init.headers).get('Content-Type')).toBe('application/json');
  });

  it('sends an explicit timeout_sec, defaulting to 15 and honouring an override', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(sseResponse(['event: complete\ndata: {"type":"complete"}\n\n'])));
    vi.stubGlobal('fetch', fetchMock);
    await run('id');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ command: 'id', timeout_sec: 15 });
    await new Promise<void>((resolve) => {
      streamFleetExec('id', [], (e) => e.type === 'complete' && resolve(), () => resolve(), 120);
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ command: 'id', timeout_sec: 120 });
  });

  it('reports the hub error message when the request is rejected', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'unknown targets: x' }), { status: 400 }))
    );
    const { error } = await run('id', ['x']);
    expect(error?.message).toBe('unknown targets: x');
  });

  it('reports a stream that ends before the run completes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse(['event: start\ndata: {"type":"start"}\n\n'])));
    const { events, error } = await run('id');
    expect(events.map((e) => e.type)).toEqual(['start']);
    expect(error?.message).toBe(ko.shell.api.streamEnded);
  });

  it('stays silent after cancel', async () => {
    const onError = vi.fn();
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))))
      )
    );
    const cancel = streamFleetExec('id', [], vi.fn(), onError);
    cancel();
    await new Promise((r) => setTimeout(r, 0));
    expect(onError).not.toHaveBeenCalled();
  });
});

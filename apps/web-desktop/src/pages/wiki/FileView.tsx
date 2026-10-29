import { useEffect, useRef, useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, fetchCodeFile, fetchCodeFileLines } from '../../api';
import { tr } from '../../i18n';
import { Caption, Card } from '../../components/Card';
import { DataGate, ErrorNotice } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { errorText } from '../../lib/format';
import { fileHref, parseLineRange, wikiStateLabel, wikiStateTone } from '../../lib/wiki';
import { hrefFor } from '../../router';
import type { CodeFileDecl, CodeFileDetail, CodeFileLines, CodeFileRef } from '../../types';

/** Lines one source request asks for (the hub serves at most 400). */
export const SOURCE_WINDOW = 120;
/** Lines of context shown before a cited range. */
const CONTEXT = 8;

/** The requested window of source lines and the lines to highlight in it. */
export interface SourceRange {
  start: number;
  end: number;
  highlight?: { start: number; end: number };
}

/** Window around a highlighted range: some context before it, the whole range (up to the hub's 400) and the rest. */
export function rangeAround(start: number, end: number, total?: number): SourceRange {
  const from = Math.max(1, start - CONTEXT);
  const upTo = Math.max(end + CONTEXT, from + SOURCE_WINDOW - 1);
  let to = Math.min(upTo, from + 399);
  if (total !== undefined && total > 0) to = Math.min(to, total);
  return { start: from, end: Math.max(from, to), highlight: { start, end } };
}

/** The declaration a `#Symbol` citation names: exact qualified name, then a qualified suffix, then the plain name. */
export function findDecl(outline: CodeFileDecl[], symbol: string): CodeFileDecl | undefined {
  return outline.find((d) => d.qualified === symbol) ?? outline.find((d) => d.qualified.endsWith(`.${symbol}`)) ?? outline.find((d) => d.name === symbol);
}

type SourceState = { phase: 'idle' } | { phase: 'loading' } | { phase: 'ok'; data: CodeFileLines } | { phase: 'error'; error: string };

function Source({
  project,
  path,
  range,
  total,
  onRange,
}: {
  project: string;
  path: string;
  range: SourceRange;
  total: number;
  onRange: (r: SourceRange) => void;
}) {
  const [state, setState] = useState<SourceState>({ phase: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const firstMarked = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setState({ phase: 'loading' });
    fetchCodeFileLines(project, path, range.start, range.end)
      .then((data) => {
        if (!cancelled) setState({ phase: 'ok', data });
      })
      .catch((err: unknown) => {
        if (!cancelled && !(err instanceof AuthRequiredError)) setState({ phase: 'error', error: errorText(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [project, path, range.start, range.end, attempt]);

  useEffect(() => {
    if (state.phase === 'ok') firstMarked.current?.scrollIntoView?.({ block: 'center' });
  }, [state]);

  if (state.phase === 'loading' || state.phase === 'idle') return <div style={{ ...muted, fontSize: 13 }}>…</div>;
  if (state.phase === 'error') {
    return <ErrorNotice title={tr.wiki.files.sourceWhat} message={state.error} onRetry={() => setAttempt((n) => n + 1)} />;
  }
  const { data } = state;
  const hl = range.highlight;
  const width = String(data.end).length;
  const span = range.end - range.start + 1;
  return (
    <Stack gap="var(--sg-s-3)">
      <Row style={{ justifyContent: 'space-between' }}>
        <span style={{ ...muted, fontSize: 12 }}>{tr.wiki.files.sourceRange(data.start, data.end, data.total)}</span>
        <Row>
          <Button
            size="sm"
            variant="outline"
            disabled={data.start <= 1}
            onClick={() =>
              onRange({
                start: Math.max(1, data.start - span),
                end: data.start - 1,
                highlight: hl,
              })
            }
          >
            {tr.wiki.files.prev}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={data.end >= total}
            onClick={() =>
              onRange({
                start: data.end + 1,
                end: Math.min(total, data.end + span),
                highlight: hl,
              })
            }
          >
            {tr.wiki.files.next}
          </Button>
        </Row>
      </Row>
      {data.truncated ? <Notice tone="warn">{tr.wiki.files.sourceTruncated}</Notice> : null}
      <div
        data-testid="file-source"
        style={{
          ...mono,
          fontSize: 12,
          lineHeight: 1.55,
          background: 'var(--sg-code-bg)',
          border: '1px solid var(--sg-border)',
          borderRadius: 'var(--sg-r-md)',
          overflow: 'auto',
          maxHeight: 'min(70vh, 720px)',
          padding: 'var(--sg-s-3) 0',
        }}
      >
        {data.lines.map((text, i) => {
          const n = data.start + i;
          const marked = !!hl && n >= hl.start && n <= hl.end;
          return (
            <div
              key={n}
              ref={marked && n === Math.max(hl!.start, data.start) ? firstMarked : undefined}
              data-line={n}
              data-marked={marked ? 'true' : undefined}
              style={{
                display: 'flex',
                whiteSpace: 'pre',
                background: marked ? 'var(--sg-info-bg)' : undefined,
              }}
            >
              <span
                style={{
                  ...muted,
                  userSelect: 'none',
                  textAlign: 'right',
                  minWidth: `${width + 2}ch`,
                  paddingRight: '1.5ch',
                }}
              >
                {n}
              </span>
              <span
                style={{
                  color: 'var(--sg-text)',
                  paddingRight: 'var(--sg-s-5)',
                }}
              >
                {text || ' '}
              </span>
            </div>
          );
        })}
      </div>
      <div style={{ ...muted, fontSize: 11 }}>{tr.wiki.files.audited}</div>
    </Stack>
  );
}

function RefList({ title, refs }: { title: string; refs: CodeFileRef[] }) {
  return (
    <Stack gap="var(--sg-s-2)">
      <Caption>{title}</Caption>
      {refs.length === 0 ? (
        <span style={{ ...muted, fontSize: 12 }}>{tr.wiki.files.noRefs}</span>
      ) : (
        <ul
          style={{
            margin: 0,
            paddingLeft: '1.2em',
            fontSize: 12,
            display: 'grid',
            gap: 2,
          }}
        >
          {refs.map((r) => (
            <li key={r.path}>
              <a href={fileHref(r.path)} style={{ ...mono, overflowWrap: 'anywhere' }}>
                {r.path}
              </a>{' '}
              <span style={muted}>{tr.wiki.files.refCount(r.count)}</span>
              {r.test ? <span style={{ ...muted, marginLeft: 6 }}>· {tr.wiki.files.testBadge}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </Stack>
  );
}

function FileBody({ project, data, lines, symbol }: { project: string; data: CodeFileDetail; lines?: string; symbol?: string }) {
  const initial = (): { range: SourceRange | null; missing: string | null } => {
    const cited = parseLineRange(lines);
    if (cited)
      return {
        range: rangeAround(cited.start, cited.end, data.lines),
        missing: null,
      };
    if (symbol) {
      const decl = findDecl(data.outline, symbol);
      return decl
        ? {
            range: rangeAround(decl.start, decl.end, data.lines),
            missing: null,
          }
        : { range: null, missing: symbol };
    }
    return { range: null, missing: null };
  };
  const [{ range, missing }, setView] = useState(initial);
  const setRange = (r: SourceRange) => setView({ range: r, missing: null });
  const fromCitation = !!(lines || symbol);
  const kb = (data.size / 1024).toFixed(1);

  return (
    <Stack gap="var(--sg-s-6)">
      <Card>
        <Stack gap="var(--sg-s-2)">
          <strong style={{ ...mono, overflowWrap: 'anywhere' }}>{data.path}</strong>
          <Row style={{ ...muted, fontSize: 12 }}>
            <span>{tr.wiki.files.meta(data.language ?? tr.wiki.files.noLanguage, data.lines, kb)}</span>
            <span>
              {tr.wiki.files.checkedAt}
              <span style={mono} title={data.sha}>
                {data.sha.slice(0, 7)}
              </span>
            </span>
          </Row>
        </Stack>
      </Card>

      <Stack gap="var(--sg-s-3)">
        <Caption>{tr.wiki.files.pagesTitle}</Caption>
        {data.pages.length === 0 ? (
          <span style={{ ...muted, fontSize: 13 }}>{tr.wiki.files.noPages}</span>
        ) : (
          <Row>
            {data.pages.map((p) => (
              <a
                key={p.id}
                href={hrefFor('debates', ['wiki', p.id])}
                style={{
                  display: 'inline-flex',
                  gap: 6,
                  alignItems: 'center',
                  fontSize: 13,
                }}
              >
                <StatusChip tone={wikiStateTone(p.state)} title={p.state}>
                  {wikiStateLabel(p.state)}
                </StatusChip>
                <span>{p.title}</span>
                <span style={{ ...muted, fontSize: 12 }}>({tr.wiki.files.via[p.via] ?? p.via})</span>
              </a>
            ))}
          </Row>
        )}
      </Stack>

      <Stack gap="var(--sg-s-3)">
        <Caption>{tr.wiki.files.docsTitle}</Caption>
        {data.docs.length === 0 ? (
          <span style={{ ...muted, fontSize: 13 }}>{tr.wiki.files.noDocs}</span>
        ) : (
          <ul
            style={{
              margin: 0,
              paddingLeft: '1.2em',
              fontSize: 13,
              display: 'grid',
              gap: 4,
            }}
          >
            {data.docs.map((d) => (
              <li key={d.path}>
                <a href={fileHref(d.path)} style={{ ...mono, fontSize: 12 }}>
                  {d.path}
                </a>
                {d.title ? <span> — {d.title}</span> : null}{' '}
                {d.lines.map((n) => (
                  <a key={n} href={fileHref(d.path, { lines: String(n) })} style={{ ...muted, fontSize: 12, marginLeft: 6 }}>
                    {tr.wiki.files.docLine(n)}
                  </a>
                ))}
              </li>
            ))}
          </ul>
        )}
      </Stack>

      {data.binary ? (
        <Notice tone="warn">{tr.wiki.files.binary}</Notice>
      ) : (
        <Stack gap="var(--sg-s-3)">
          <Caption>{tr.wiki.files.sourceTitle}</Caption>
          {missing ? <Notice tone="warn">{tr.wiki.files.symbolMissing(missing)}</Notice> : null}
          {range && fromCitation ? <div style={{ ...muted, fontSize: 12 }}>{tr.wiki.files.lineShift}</div> : null}
          {range ? (
            <Source key={`${range.start}-${range.end}`} project={project} path={data.path} range={range} total={data.lines} onRange={setRange} />
          ) : (
            <Row>
              <span style={{ ...muted, fontSize: 13 }}>{tr.wiki.files.sourceHint}</span>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  setRange({
                    start: 1,
                    end: Math.min(Math.max(data.lines, 1), SOURCE_WINDOW),
                  })
                }
              >
                {tr.wiki.files.showTop}
              </Button>
            </Row>
          )}
        </Stack>
      )}

      <Stack gap="var(--sg-s-3)">
        <Caption>{tr.wiki.files.outlineTitle(data.outline.length)}</Caption>
        {data.outline.length === 0 ? (
          <span style={{ ...muted, fontSize: 13 }}>{tr.wiki.files.noOutline}</span>
        ) : (
          <ul
            style={{
              margin: 0,
              padding: 0,
              listStyle: 'none',
              fontSize: 12,
              display: 'grid',
              gap: 2,
              maxHeight: 360,
              overflowY: 'auto',
            }}
          >
            {data.outline.map((d) => (
              <li key={`${d.qualified}:${d.start}`}>
                <button
                  type="button"
                  aria-label={tr.wiki.files.showDecl(d.qualified)}
                  onClick={() => setRange(rangeAround(d.start, d.end, data.lines))}
                  title={d.signature}
                  style={{
                    all: 'unset',
                    cursor: 'pointer',
                    display: 'flex',
                    gap: 8,
                    width: '100%',
                    ...mono,
                  }}
                >
                  <span style={{ ...muted, minWidth: '6ch' }}>{d.kind}</span>
                  <span
                    style={{
                      color: 'var(--sg-accent)',
                      overflowWrap: 'anywhere',
                    }}
                  >
                    {d.qualified}
                  </span>
                  <span style={{ ...muted, marginLeft: 'auto' }}>{d.start === d.end ? d.start : `${d.start}–${d.end}`}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Stack>

      <Stack gap="var(--sg-s-4)">
        <div
          style={{
            display: 'grid',
            gap: 'var(--sg-s-5)',
            gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
          }}
        >
          <RefList title={tr.wiki.files.incomingTitle(data.incoming.length)} refs={data.incoming} />
          <RefList title={tr.wiki.files.outgoingTitle(data.outgoing.length)} refs={data.outgoing} />
        </div>
        <div style={{ ...muted, fontSize: 11 }}>{tr.wiki.files.refsNote}</div>
      </Stack>
    </Stack>
  );
}

/** One repository file without an LLM: wiki pages and documents that cover it, its source, declarations and references. */
export function FileView({ project, path, lines, symbol }: { project: string; path: string; lines?: string; symbol?: string }) {
  const state = useHubData<CodeFileDetail>(() => fetchCodeFile(project, path));
  return (
    <DataGate state={state} what={tr.wiki.files.what(path)}>
      {(data) => <FileBody key={`${lines ?? ''}#${symbol ?? ''}`} project={project} data={data} lines={lines} symbol={symbol} />}
    </DataGate>
  );
}

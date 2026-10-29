import type { ReactNode } from 'react';
import { CodeBlock, StatusChip } from '@server-guardian/ui';
import { Caption, Card } from '../../components/Card';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { DiffView } from '../../components/DiffView';
import { Disclosure } from '../../components/Disclosure';
import { MarkdownDoc } from '../../components/MarkdownDoc';
import { MonoBlock } from '../../components/MonoBlock';
import { Notice } from '../../components/Notice';
import { Stack, mono, muted } from '../../components/layout';
import { artifactViewForName, mermaidAsMarkdown, svgDataUrl } from '../../lib/artifacts';
import { jevGateTone, patchEdits, patchSummary, pretty } from '../../lib/debate';
import { fmtBytes } from '../../lib/format';
import type { ActionCheckResult, ActionItem, ActionPreview, JevRecheckResponse } from '../../types';
import { tr } from '../../i18n';

function Labeled({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-2)', minWidth: 0 }}>
      <Caption>{label}</Caption>
      {children}
    </div>
  );
}

function OtherArguments({ rest }: { rest: Record<string, unknown> }) {
  if (Object.keys(rest).length === 0) return null;
  return (
    <Labeled label={tr.debate.details.otherArgs}>
      <CodeBlock code={pretty(rest)} copyable={false} />
    </Labeled>
  );
}

function PatchArguments({ action }: { action: ActionItem }) {
  const args = (action.arguments || {}) as Record<string, unknown>;
  const edits = patchEdits(action);
  const { edits: _edits, ...rest } = args;
  return (
    <Stack gap="var(--sg-s-4)">
      <strong style={{ fontSize: 13 }}>{patchSummary(action)}</strong>
      <Labeled label={tr.debate.details.changesVsBase}>
        {action.diff ? <DiffView diff={action.diff} /> : <span style={muted}>{tr.debate.details.diffUnavailable}</span>}
      </Labeled>
      <Disclosure summary={tr.debate.details.rawEdits}>
        {edits.length === 0 ? (
          <span style={muted}>{tr.debate.details.noEdits}</span>
        ) : (
          edits.map((e, i) => (
            <Card key={i} style={{ background: 'var(--sg-surface)' }}>
              <div style={{ ...mono, fontSize: 12, overflowWrap: 'anywhere' }}>
                {`#${i + 1} ${String(e?.file_path ?? tr.debate.details.noFilePath)}${e?.old === '' ? tr.debate.details.newFile : ''}`}
              </div>
              <Caption>{tr.debate.details.old}</Caption>
              <MonoBlock text={String(e?.old ?? '')} maxHeight={160} />
              <Caption>{tr.debate.details.new}</Caption>
              <MonoBlock text={String(e?.new ?? '')} maxHeight={160} />
            </Card>
          ))
        )}
      </Disclosure>
      <OtherArguments rest={rest} />
    </Stack>
  );
}

function CodeActionArguments({ action }: { action: ActionItem }) {
  const args = (action.arguments || {}) as Record<string, unknown>;
  const { content, file_path, run_test, ...rest } = args;
  return (
    <Stack gap="var(--sg-s-4)">
      {action.diff ? (
        <Labeled label={tr.debate.details.changesVsBase}>
          <DiffView diff={action.diff} />
        </Labeled>
      ) : null}
      <Labeled label="file_path">
        <MonoBlock text={String(file_path ?? tr.debate.details.none)} maxHeight={80} />
      </Labeled>
      <Labeled label="run_test">
        <MonoBlock text={String(run_test ?? tr.debate.details.none)} maxHeight={80} />
      </Labeled>
      <Labeled label={tr.debate.details.contentFull}>
        <MonoBlock text={typeof content === 'string' ? content : content === undefined ? tr.debate.details.none : pretty(content)} maxHeight={320} />
      </Labeled>
      <OtherArguments rest={rest} />
    </Stack>
  );
}

/**
 * A `write_artifact` proposal shows the document as it will look once stored, drawn like the Artifacts tab does
 * (Markdown and Mermaid through MarkdownDoc, SVG only as an image). The text is a model's output: it is only ever
 * sanitized by MarkdownDoc or put into an `<img>`. The raw text stays one click away.
 */
function ArtifactArguments({ action }: { action: ActionItem }) {
  const args = (action.arguments || {}) as Record<string, unknown>;
  const { name, content, base_version, ...rest } = args;
  const view = artifactViewForName(name);
  const text = typeof content === 'string' ? content : null;
  let preview: ReactNode = <span style={muted}>{tr.debate.details.none}</span>;
  if (text !== null && view === 'svg') {
    preview = <img src={svgDataUrl(text)} alt={tr.artifacts.svgAlt(String(name))} style={{ maxWidth: '100%', height: 'auto' }} />;
  } else if (text !== null && view) {
    preview = <MarkdownDoc markdown={view === 'mermaid' ? mermaidAsMarkdown(text) : text} topHeading={2} />;
  }
  return (
    <Stack gap="var(--sg-s-4)">
      <Labeled label={tr.debate.details.artifactName}>
        <MonoBlock text={String(name ?? tr.debate.details.none)} maxHeight={80} />
      </Labeled>
      <Labeled label={tr.debate.details.artifactBase}>
        <MonoBlock text={typeof base_version === 'string' ? base_version : tr.debate.details.artifactFirstSave} maxHeight={80} />
      </Labeled>
      <Labeled label={tr.debate.details.artifactPreview}>
        <div style={{ maxHeight: 480, overflow: 'auto', padding: 'var(--sg-s-4) var(--sg-s-5)', border: '1px solid var(--sg-border)', borderRadius: 'var(--sg-r-md)', background: 'var(--sg-surface)' }}>
          {preview}
        </div>
      </Labeled>
      <Disclosure summary={tr.debate.details.artifactRaw}>
        <MonoBlock text={text ?? (content === undefined ? tr.debate.details.none : pretty(content))} maxHeight={320} />
      </Disclosure>
      <OtherArguments rest={rest} />
    </Stack>
  );
}

/** The action's arguments in full; `apply_patch` and `execute_code_action` get a diff view, `write_artifact` a preview. */
export function ActionArguments({ action }: { action: ActionItem }) {
  if (action.tool === 'jev_mcp:apply_patch') return <PatchArguments action={action} />;
  if (action.tool === 'jev_mcp:execute_code_action') return <CodeActionArguments action={action} />;
  if (action.tool === 'jev_mcp:write_artifact') return <ArtifactArguments action={action} />;
  return <CodeBlock code={pretty(action.arguments || {})} copyable={false} />;
}

// --- Previous result ---------------------------------------------------------------------------

const CHECK_COLUMNS: Column<ActionCheckResult>[] = [
  { header: 'cmd', mono: true, render: (c) => String(c?.cmd ?? '') },
  { header: 'cwd', mono: true, render: (c) => String(c?.cwd ?? '.') },
  {
    header: 'exit',
    mono: true,
    render: (c) => <span style={{ fontWeight: 700, color: c?.exit_code === 0 ? 'var(--sg-ok)' : 'var(--sg-crit)' }}>{String(c?.exit_code)}</span>,
  },
  {
    header: tr.debate.details.output,
    render: (c) =>
      c?.stdout || c?.stderr ? (
        <Disclosure summary={tr.debate.details.output}>
          {c.stdout ? <MonoBlock text={c.stdout} maxHeight={200} /> : null}
          {c.stderr ? <MonoBlock text={c.stderr} maxHeight={200} tone="warn" /> : null}
        </Disclosure>
      ) : (
        <span style={muted}>-</span>
      ),
  },
];

/** Result of a run: reverted banner, exit code, message / error, applied diff, checks, stdout / stderr, raw JSON. */
/** Before / after measurement of an ops cleanup action. */
function VerifyLine({ verify }: { verify: Record<string, unknown> }) {
  return (
    <Labeled label={tr.debate.details.verify}>
      <Stack gap="var(--sg-s-2)">
        <div style={{ ...mono, fontSize: 13 }}>{tr.debate.details.verifyLine(size(verify.before), size(verify.after), size(verify.freed))}</div>
        {verify.ok === false ? <Notice tone="warn">{tr.debate.details.verifyFailed}</Notice> : null}
      </Stack>
    </Labeled>
  );
}

function size(bytes: unknown): string {
  return typeof bytes === 'number' ? fmtBytes(bytes) : tr.debate.details.unknownSize;
}

/** Package actions: what is protected on the node and, when the node hides some owners, why that list may be short. */
export function ProtectionNotes({ preview }: { preview: ActionPreview }) {
  const protectedList = preview.protected ?? [];
  const blind = preview.blind ?? [];
  if (!protectedList.length && !blind.length) return null;
  return (
    <Stack gap="var(--sg-s-2)">
      {blind.length ? (
        <Notice tone="warn">
          {tr.debate.details.blindSpots}
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {blind.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {protectedList.length ? (
        <div style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{tr.debate.details.protectedHere(protectedList.join(', '))}</div>
      ) : null}
    </Stack>
  );
}

/** What an ops cleanup action would delete, measured when it was proposed. */
export function ActionPreviewView({ preview }: { preview: ActionPreview }) {
  return (
    <Stack gap="var(--sg-s-2)">
      <div style={{ lineHeight: 1.6 }}>
        {typeof preview.bytes === 'number' ? tr.debate.details.previewFrees(fmtBytes(preview.bytes)) : tr.debate.details.previewUnknown}
      </div>
      {preview.error ? <Notice tone="warn">{preview.error}</Notice> : null}
      <ProtectionNotes preview={preview} />
      {preview.text ? <MonoBlock text={preview.text} maxHeight={160} /> : null}
    </Stack>
  );
}

export function ActionResult({ result }: { result: unknown }) {
  if (result === null || result === undefined || result === '') return null;
  const raw = (
    <Disclosure summary={tr.debate.details.rawJson}>
      <CodeBlock code={pretty(result)} copyable={false} />
    </Disclosure>
  );
  if (typeof result !== 'object' || Array.isArray(result)) {
    return (
      <Stack gap="var(--sg-s-3)">
        <MonoBlock text={typeof result === 'string' ? result : pretty(result)} />
      </Stack>
    );
  }
  const r = result as Record<string, unknown>;
  const meta: string[] = [];
  if (r.exit_code !== undefined) meta.push(`exit_code: ${String(r.exit_code)}`);
  if (r.success !== undefined) meta.push(`success: ${String(r.success)}`);
  return (
    <Stack gap="var(--sg-s-4)">
      {r.reverted === true ? <Notice tone="crit">{tr.debate.details.reverted}</Notice> : null}
      {r.verify && typeof r.verify === 'object' ? <VerifyLine verify={r.verify as Record<string, unknown>} /> : null}
      {meta.length ? <div style={{ ...mono, fontSize: 12 }}>{meta.join('   ')}</div> : null}
      {r.message ? (
        <Labeled label={tr.debate.details.message}>
          <MonoBlock text={String(r.message)} maxHeight={160} />
        </Labeled>
      ) : null}
      {r.error ? (
        <Labeled label={tr.debate.details.error}>
          <MonoBlock text={typeof r.error === 'string' ? r.error : pretty(r.error)} maxHeight={160} tone="crit" />
        </Labeled>
      ) : null}
      {typeof r.diff === 'string' && r.diff ? (
        <Labeled label={tr.debate.details.appliedChanges}>
          <DiffView diff={r.diff} label={tr.debate.details.appliedDiff} />
        </Labeled>
      ) : null}
      {Array.isArray(r.checks) && r.checks.length > 0 ? (
        <Labeled label={tr.debate.details.checks}>
          <DataTable label={tr.debate.details.checks} columns={CHECK_COLUMNS} rows={r.checks as ActionCheckResult[]} getKey={(_, i) => String(i)} />
        </Labeled>
      ) : null}
      {r.stdout ? (
        <Labeled label="stdout">
          <MonoBlock text={String(r.stdout)} />
        </Labeled>
      ) : null}
      {r.stderr ? (
        <Labeled label="stderr">
          <MonoBlock text={String(r.stderr)} tone="warn" />
        </Labeled>
      ) : null}
      {raw}
    </Stack>
  );
}

/** Outcome of a jev re-check: gate, released action ids, reasons. */
export function RecheckSummary({ result }: { result: JevRecheckResponse }) {
  const reasons = result.review?.reasons || [];
  const released = result.released || [];
  return (
    <Card>
      <div style={{ fontWeight: 700 }}>
        {`${tr.debate.details.jevGate}: `}
        <StatusChip tone={jevGateTone(result.gate)}>{result.gate}</StatusChip>
      </div>
      <div style={{ fontSize: 13 }}>
        {`${tr.debate.details.released}: `}
        {released.length ? <span style={mono}>{released.map((x) => `#${String(x)}`).join(', ')}</span> : tr.common.none}
      </div>
      {reasons.length ? (
        <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', lineHeight: 1.5, fontSize: 13 }}>
          {reasons.map((x, i) => (
            <li key={i} style={{ overflowWrap: 'anywhere' }}>
              {x}
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

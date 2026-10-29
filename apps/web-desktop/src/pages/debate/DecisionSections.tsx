import { useState } from 'react';
import type { CSSProperties } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { Caption, Card } from '../../components/Card';
import { Disclosure } from '../../components/Disclosure';
import { MarkdownDoc } from '../../components/MarkdownDoc';
import { Row, Stack, mono, muted } from '../../components/layout';
import {
  REQ_ID_RE,
  actionStatusTone,
  basename,
  dirname,
  extractJudgmentJson,
  fmtKB,
  parseDecision,
  requirementLinks,
  splitRationale,
  structureDecision,
} from '../../lib/debate';
import type { FileChange, ParsedDecision } from '../../lib/debate';
import type { DebateFinalDecision } from '../../types';
import { tr } from '../../i18n';

const R = tr.debate.result;

const plain: CSSProperties = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', lineHeight: 1.6, margin: 0 };
const list: CSSProperties = { margin: 0, paddingLeft: 'var(--sg-s-9)', lineHeight: 1.6, overflowWrap: 'anywhere' };
const badge: CSSProperties = {
  ...mono,
  fontSize: 11,
  color: 'var(--sg-text-secondary)',
  background: 'var(--sg-code-bg)',
  border: '1px solid var(--sg-border)',
  borderRadius: 4,
  padding: '0 5px',
  whiteSpace: 'nowrap',
};
const codeBlock: CSSProperties = {
  ...mono,
  fontSize: 12,
  lineHeight: 1.5,
  margin: 0,
  padding: 'var(--sg-s-4) var(--sg-s-5)',
  background: 'var(--sg-code-bg)',
  border: '1px solid var(--sg-border)',
  borderRadius: 'var(--sg-r-md)',
  overflow: 'auto',
  maxHeight: 360,
  color: 'var(--sg-text-secondary)',
  whiteSpace: 'pre',
};

/** Requirement ids a constraint or check traces back to. */
function RefBadges({ refs }: { refs: string[] | undefined }) {
  if (!refs || refs.length === 0) return null;
  return (
    <>
      {refs.map((r) => (
        <span key={r} style={{ ...badge, marginLeft: 'var(--sg-s-2)' }} title={R.requirementCard(r)}>
          {r}
        </span>
      ))}
    </>
  );
}

function RequirementCard({
  req,
  fd,
  files,
  traced,
}: {
  req: ParsedDecision['requirements'][number];
  fd: DebateFinalDecision;
  files: FileChange[];
  traced: boolean;
}) {
  const links = traced && REQ_ID_RE.test(req.id) ? requirementLinks(req.id, fd, files) : null;
  return (
    <Card style={{ alignContent: 'start' }}>
      <Row gap="var(--sg-s-3)" style={{ alignItems: 'baseline' }}>
        <span style={{ ...badge, fontSize: 12 }}>{req.id}</span>
        {req.title ? <strong style={{ fontSize: 14, overflowWrap: 'anywhere' }}>{req.title}</strong> : null}
      </Row>
      <MarkdownDoc markdown={req.text} />
      {links ? (
        <Stack gap="var(--sg-s-2)">
          <Row gap="var(--sg-s-3)" style={{ fontSize: 12, ...muted }}>
            <span>{R.linkConstraints(links.constraints.length)}</span>
            <span>{R.linkChecks(links.checks.length)}</span>
            <span>{R.linkFiles(links.files.length)}</span>
          </Row>
          {links.files.length ? (
            <ul style={{ ...list, paddingLeft: 'var(--sg-s-7)', fontSize: 12, ...mono }}>
              {links.files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          ) : (
            <div>
              <StatusChip tone="warn">{R.noLinkedChange}</StatusChip>
            </div>
          )}
        </Stack>
      ) : null}
    </Card>
  );
}

/** Summary, requirement cards, extra decisions and the closing callout; plain Markdown when no item marker exists. */
export function DecisionView({ fd, files, traced }: { fd: DebateFinalDecision; files: FileChange[]; traced: boolean }) {
  const decision = fd.decision ?? '';
  const parsed = parseDecision(decision);
  if (!parsed) {
    return (
      <Stack gap="var(--sg-s-2)">
        <Caption>{R.finalDecision}</Caption>
        <MarkdownDoc markdown={structureDecision(decision)} />
      </Stack>
    );
  }
  return (
    <Stack gap="var(--sg-s-4)">
      <Caption>{R.finalDecision}</Caption>
      {parsed.intro ? (
        <Stack gap="var(--sg-s-2)">
          <Caption>{R.summary}</Caption>
          <MarkdownDoc markdown={parsed.intro} />
        </Stack>
      ) : null}
      {parsed.requirements.length ? (
        <section aria-label={R.requirementsLabel} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 'var(--sg-s-5)', minWidth: 0 }}>
          {parsed.requirements.map((req) => (
            <RequirementCard key={req.id} req={req} fd={fd} files={files} traced={traced} />
          ))}
        </section>
      ) : null}
      {parsed.extras.length ? (
        <Stack gap="var(--sg-s-2)">
          <Caption>{R.extras}</Caption>
          <ul style={list}>
            {parsed.extras.map((x, i) => (
              <li key={i}>
                {x.title ? <strong>{`${x.title}: `}</strong> : null}
                {x.text}
              </li>
            ))}
          </ul>
        </Stack>
      ) : null}
      {parsed.conclusion ? (
        <div style={{ padding: 'var(--sg-s-5) var(--sg-s-6)', background: 'var(--sg-surface-raised)', border: '1px solid var(--sg-info-border)', borderRadius: 'var(--sg-r-card)' }}>
          <Caption>{R.conclusion}</Caption>
          <MarkdownDoc markdown={parsed.conclusion} />
        </div>
      ) : null}
    </Stack>
  );
}

/** "판단 근거": numbered items as titled blocks, `(a) (b) (c)` alternatives as a sub-list. */
export function RationaleSection({ text }: { text: string }) {
  const items = splitRationale(text);
  if (items.length === 0) return null;
  return (
    <Stack gap="var(--sg-s-3)">
      <Caption>{R.rationale}</Caption>
      <Stack gap="var(--sg-s-4)">
        {items.map((it, i) => (
          <Card key={i}>
            <Row gap="var(--sg-s-3)" style={{ alignItems: 'baseline' }}>
              {it.num ? <span style={badge}>{it.num}</span> : null}
              {it.title ? <strong style={{ fontSize: 14 }}>{it.title}</strong> : null}
            </Row>
            {it.text ? <p style={plain}>{it.text}</p> : null}
            {it.alternatives.length ? (
              <>
                <Caption>{R.alternatives}</Caption>
                <ul style={list}>
                  {it.alternatives.map((a) => (
                    <li key={a.label}>
                      <span style={badge}>{a.label}</span> {a.text}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </Card>
        ))}
      </Stack>
    </Stack>
  );
}

/** A captioned bullet list whose items may carry requirement-id badges. */
export function RefList({ caption, items, refs }: { caption: string; items: string[]; refs?: string[][] }) {
  if (items.length === 0) return null;
  return (
    <Stack gap="var(--sg-s-2)">
      <Caption>{caption}</Caption>
      <ul style={list}>
        {items.map((c, i) => (
          <li key={i}>
            {c}
            <RefBadges refs={refs?.[i]} />
          </li>
        ))}
      </ul>
    </Stack>
  );
}

/** Readable overview of the files the actions write, plus files the decision names that nothing writes. */
/**
 * Files the judge's actions write, grouped by directory. The section folds as a whole, and each file is a card
 * whose header (name, status, refs) stays visible while its path, reason and content fold; a folded file's
 * content is not rendered. Files start folded.
 */
export function FileChangesSection({ changes, unwritten }: { changes: FileChange[]; unwritten: string[] }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  if (changes.length === 0 && unwritten.length === 0) return null;
  const groups = new Map<string, FileChange[]>();
  for (const c of changes) {
    const d = dirname(c.path);
    groups.set(d, [...(groups.get(d) ?? []), c]);
  }
  const keyOf = (c: FileChange) => `${c.id}:${c.path}`;
  const all = changes.map(keyOf);
  const setMany = (keys: string[], on: boolean) =>
    setOpen((prev) => {
      const next = new Set(prev);
      keys.forEach((k) => (on ? next.add(k) : next.delete(k)));
      return next;
    });
  return (
    <details open style={{ minWidth: 0 }}>
      <summary style={{ cursor: 'pointer' }} aria-label={R.filesToggle}>
        <span style={{ display: 'inline-block', verticalAlign: 'middle' }}>
          <Caption>{R.filesTitle(changes.length)}</Caption>
        </span>
      </summary>
      <Stack gap="var(--sg-s-3)" style={{ marginTop: 'var(--sg-s-3)' }}>
        {all.length > 1 ? (
          <Row style={{ justifyContent: 'flex-end' }} gap="var(--sg-s-3)">
            <Button size="sm" variant="outline" onClick={() => setMany(all, true)}>
              {R.filesExpandAll}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setMany(all, false)}>
              {R.filesCollapseAll}
            </Button>
          </Row>
        ) : null}
        <div role="list" aria-label={R.filesLabel} style={{ display: 'grid', gap: 'var(--sg-s-4)', minWidth: 0 }}>
          {[...groups.entries()].map(([dir, items]) => (
            <Stack key={dir} gap="var(--sg-s-3)">
              <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{dir || R.rootDir}</div>
              {items.map((c) => {
                const key = keyOf(c);
                const isOpen = open.has(key);
                return (
                  <div key={key} role="listitem">
                    <Card>
                      <details open={isOpen} onToggle={(e) => setMany([key], e.currentTarget.open)} style={{ minWidth: 0 }}>
                        <summary style={{ cursor: 'pointer' }} aria-label={R.fileToggle(c.path)}>
                          <span style={{ display: 'inline-flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--sg-s-3)', verticalAlign: 'middle' }}>
                            <strong style={{ ...mono, fontSize: 13, overflowWrap: 'anywhere' }}>{basename(c.path)}</strong>
                            <StatusChip tone={actionStatusTone(c.status)} title={c.status}>
                              {tr.debate.actionStatus[c.status.toUpperCase()] ?? c.status}
                            </StatusChip>
                            {c.noTest ? <StatusChip tone="warn">{R.noTest}</StatusChip> : null}
                            {c.content !== null ? <span style={{ ...muted, fontSize: 12 }}>{fmtKB(c.bytes)}</span> : null}
                            <span style={{ ...mono, ...muted, fontSize: 12 }}>{`#${c.id}`}</span>
                            {c.refs.map((r) => (
                              <span key={r} style={badge}>
                                {r}
                              </span>
                            ))}
                          </span>
                        </summary>
                        {isOpen ? (
                          <Stack gap="var(--sg-s-3)" style={{ marginTop: 'var(--sg-s-3)' }}>
                            <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{c.path}</div>
                            {c.reason ? <div style={{ fontSize: 13, lineHeight: 1.6, overflowWrap: 'anywhere' }}>{c.reason}</div> : null}
                            {c.content !== null ? (
                              <pre style={codeBlock}>
                                <code>{c.content}</code>
                              </pre>
                            ) : null}
                          </Stack>
                        ) : null}
                      </details>
                    </Card>
                  </div>
                );
              })}
            </Stack>
          ))}
          {unwritten.map((f) => (
            <div key={`none:${f}`} role="listitem">
              <Card tone="warn">
                <Row gap="var(--sg-s-3)">
                  <strong style={{ ...mono, fontSize: 13, overflowWrap: 'anywhere' }}>{f}</strong>
                  <StatusChip tone="warn">{R.noChangeAction}</StatusChip>
                </Row>
                <div style={{ ...muted, fontSize: 12 }}>{R.noChangeNote}</div>
              </Card>
            </div>
          ))}
        </div>
      </Stack>
    </details>
  );
}

/** The judge's reasoning without its machine-readable fences, which sit behind "원본 JSON 보기". */
export function JudgmentSection({ judgment }: { judgment: string | undefined }) {
  const split = judgment ? extractJudgmentJson(judgment) : null;
  return (
    <Stack gap="var(--sg-s-2)">
      <Caption>{R.judgment}</Caption>
      {split ? (
        <>
          {split.markdown ? <MarkdownDoc markdown={split.markdown} topHeading={4} /> : null}
          {split.blocks.length ? (
            <Disclosure summary={R.rawJson}>
              {split.blocks.map((b, i) => (
                <Stack key={i} gap="var(--sg-s-2)">
                  <Caption>{b.kind === 'decision' ? R.rawDecision : R.rawFindings}</Caption>
                  <pre style={codeBlock}>
                    <code>{b.text}</code>
                  </pre>
                </Stack>
              ))}
            </Disclosure>
          ) : null}
        </>
      ) : (
        <p style={plain}>{R.noJudgment}</p>
      )}
    </Stack>
  );
}

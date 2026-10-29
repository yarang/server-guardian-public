import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { AuthRequiredError, deleteWikiPage, fetchWikiPage } from '../../api';
import { Caption, Card } from '../../components/Card';
import { ConfirmModal } from '../../components/ConfirmModal';
import { DataGate } from '../../components/DataGate';
import { Disclosure } from '../../components/Disclosure';
import { MonoBlock } from '../../components/MonoBlock';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { hrefFor } from '../../router';
import { useHubData } from '../../hooks/useHubData';
import { errorText } from '../../lib/format';
import { authorText, fileHref, isHumanAuthor, wikiStateLabel, wikiStateTone } from '../../lib/wiki';
import type { PageLinks } from '../../lib/wikiCitations';
import type { WikiPageDetail, WikiStaleness } from '../../types';
import { WikiEditDialog } from './WikiEditDialog';
import { WikiNarrative } from './WikiNarrative';

const scopeChip = {
  ...mono,
  fontSize: 12,
  padding: '2px var(--sg-s-3)',
  background: 'var(--sg-code-bg)',
  border: '1px solid var(--sg-border)',
  borderRadius: 'var(--sg-r-md)',
  color: 'var(--sg-text)',
  textDecoration: 'none',
} as const;

function PathList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <div style={{ fontWeight: 600 }}>{title}</div>
      <div style={{ ...mono, fontSize: 12, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{items.map((p) => `- ${p}`).join('\n')}</div>
    </div>
  );
}

/** Why the page is stale or broken: files changed since it was written, new files, broken citations. */
function Staleness({ s }: { s: WikiStaleness }) {
  const hasDetail = s.changed.length + s.removed.length + s.new.length + s.broken.length + s.unverified.length + s.notes.length > 0;
  if (s.state === 'fresh' && !hasDetail) return null;
  return (
    <Notice tone={s.state === 'broken' ? 'crit' : s.state === 'stale' ? 'warn' : 'info'}>
      <Stack gap="var(--sg-s-3)">
        <strong>
          {s.state === 'broken' ? tr.wiki.staleness.broken : s.state === 'stale' ? tr.wiki.staleness.stale : tr.wiki.staleness.notes}
        </strong>
        <PathList title={tr.wiki.staleness.changed} items={s.changed} />
        <PathList title={tr.wiki.staleness.removed} items={s.removed} />
        <PathList title={tr.wiki.staleness.newFiles} items={s.new} />
        {s.broken.length ? (
          <div>
            <div style={{ fontWeight: 600 }}>{tr.wiki.staleness.brokenCitations}</div>
            <div style={{ ...mono, fontSize: 12, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
              {s.broken.map((b) => `- ${b.citation} (${b.reason})`).join('\n')}
            </div>
          </div>
        ) : null}
        <PathList title={tr.wiki.staleness.unverified} items={s.unverified} />
        {s.notes.length ? <div style={{ ...muted, fontSize: 12 }}>{s.notes.join('\n')}</div> : null}
      </Stack>
    </Notice>
  );
}

/**
 * Rewrite, Edit and Delete behind one button: most visits only read. A disclosure, not a `role="menu"`, so the
 * buttons stay ordinary and keep their own labels. It closes on a choice, on Escape and on a click outside.
 */
function PageActions({
  pageId,
  busy,
  onRewrite,
  onEdit,
  onDelete,
}: {
  pageId: string;
  busy: boolean;
  onRewrite: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (e.target instanceof Node && !box.current?.contains(e.target)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const choose = (action: () => void) => () => {
    setOpen(false);
    action();
  };

  return (
    <div ref={box} style={{ position: 'relative' }}>
      <Button size="sm" variant="outline" aria-expanded={open} onClick={() => setOpen(!open)}>
        {tr.wiki.viewer.pageActions}
      </Button>
      {open ? (
        <div
          style={{
            position: 'absolute',
            right: 0,
            zIndex: 5,
            marginTop: 'var(--sg-s-2)',
            display: 'grid',
            gap: 'var(--sg-s-3)',
            padding: 'var(--sg-s-4)',
            background: 'var(--sg-surface-raised)',
            border: '1px solid var(--sg-border)',
            borderRadius: 'var(--sg-r-md)',
          }}
        >
          <Button size="sm" variant="outline" disabled={busy} aria-label={tr.wiki.page.rewriteAria(pageId)} onClick={choose(onRewrite)}>
            {tr.wiki.page.rewrite}
          </Button>
          <Button size="sm" variant="outline" aria-label={tr.wiki.page.editAria(pageId)} onClick={choose(onEdit)}>
            {tr.wiki.page.edit}
          </Button>
          <Button size="sm" variant="danger" aria-label={tr.wiki.page.deleteAria(pageId)} onClick={choose(onDelete)}>
            {tr.wiki.page.delete}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function PageBody({
  project,
  data,
  pages,
  busy,
  onChanged,
  onDeleted,
  onRewrite,
}: {
  project: string;
  data: WikiPageDetail;
  pages: PageLinks;
  busy: boolean;
  onChanged: () => void;
  onDeleted: (id: string) => void;
  onRewrite: (page: string, force: boolean) => void;
}) {
  const { page } = data;
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmRewrite, setConfirmRewrite] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const human = isHumanAuthor(page.author);

  const remove = async () => {
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteWikiPage(project, page.id);
      onDeleted(page.id);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setDeleteError(errorText(err));
      setDeleteBusy(false);
    }
  };

  return (
    <Stack gap="var(--sg-s-6)">
      <Card tone={wikiStateTone(data.state)}>
        <Row style={{ justifyContent: 'space-between' }}>
          <Row>
            <StatusChip tone={wikiStateTone(data.state)} title={data.state}>
              {wikiStateLabel(data.state)}
            </StatusChip>
            <strong style={{ overflowWrap: 'anywhere' }}>{page.title}</strong>
          </Row>
          <PageActions
            pageId={page.id}
            busy={busy}
            onRewrite={() => (human ? setConfirmRewrite(true) : onRewrite(page.id, false))}
            onEdit={() => setEditing(true)}
            onDelete={() => setConfirmDelete(true)}
          />
        </Row>
        <Row gap="var(--sg-s-6)" style={{ fontSize: 12, color: 'var(--sg-text-secondary)' }}>
          <span style={mono}>{page.id}</span>
          <span>{tr.wiki.page.by(authorText(page.author))}</span>
          {page.updated ? <span>{tr.wiki.page.updated(page.updated)}</span> : null}
          {page.written_sha ? (
            <span>
              {tr.wiki.page.writtenAt}
              <span style={mono} title={page.written_sha}>
                {page.written_sha.slice(0, 7)}
              </span>
            </span>
          ) : null}
          <span>
            {tr.wiki.page.checkedAt}
            <span style={mono} title={data.sha}>
              {data.sha.slice(0, 7)}
            </span>
          </span>
        </Row>
        {page.scope.length ? (
          <Row gap="var(--sg-s-3)">
            <span style={{ ...muted, fontSize: 12 }}>{tr.wiki.page.scope}</span>
            {page.scope.map((s) =>
              s.endsWith('/') ? (
                <span key={s} style={scopeChip}>
                  {s}
                </span>
              ) : (
                <a key={s} href={fileHref(s)} style={scopeChip}>
                  {s}
                </a>
              )
            )}
          </Row>
        ) : null}
        {page.docs && page.docs.length ? (
          <Row gap="var(--sg-s-3)">
            <span style={{ ...muted, fontSize: 12 }}>{tr.wiki.page.docs}</span>
            {page.docs.map((d) => (
              <a key={d} href={fileHref(d)} style={scopeChip}>
                {d}
              </a>
            ))}
          </Row>
        ) : null}
      </Card>

      <Staleness s={data.staleness} />

      <WikiNarrative pageId={page.id} title={page.title} author={authorText(page.author)} markdown={data.narrative} pages={pages} />

      <Stack gap="var(--sg-s-3)">
        <Caption>{tr.wiki.page.generated}</Caption>
        <Disclosure summary={tr.wiki.page.repoMap(data.sha.slice(0, 7))}>
          <MonoBlock text={data.generated || tr.wiki.page.empty} maxHeight={360} />
        </Disclosure>
      </Stack>

      {editing ? (
        <WikiEditDialog
          project={project}
          isNew={false}
          draft={{ id: page.id, title: page.title, scope: page.scope, body: data.narrative }}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
        />
      ) : null}
      {confirmDelete ? (
        <ConfirmModal
          title={tr.wiki.page.deleteTitle}
          message={tr.wiki.page.deleteMessage(page.id, human)}
          confirmLabel={tr.wiki.page.deleteLabel}
          busy={deleteBusy}
          error={deleteError}
          onConfirm={() => void remove()}
          onCancel={() => {
            setConfirmDelete(false);
            setDeleteError(null);
          }}
        />
      ) : null}
      {confirmRewrite ? (
        <ConfirmModal
          title={tr.wiki.page.rewriteTitle}
          message={tr.wiki.page.rewriteMessage(page.id, authorText(page.author))}
          confirmLabel={tr.wiki.page.rewriteLabel}
          tone="primary"
          onConfirm={() => {
            setConfirmRewrite(false);
            onRewrite(page.id, true);
          }}
          onCancel={() => setConfirmRewrite(false)}
        />
      ) : null}
    </Stack>
  );
}

/** One page: header with state, staleness details, narrative and generated overview, with edit, delete and rewrite. */
export function WikiPageView({
  project,
  page,
  knownPages,
  busy,
  onChanged,
  onDeleted,
  onRewrite,
}: {
  project: string;
  page: string;
  /** Ids of the pages in the list, once it has loaded; `[[id]]` links to others are marked missing against it. */
  knownPages: readonly string[] | null;
  /** A build is running or being started; Rewrite waits. */
  busy: boolean;
  onChanged: () => void;
  onDeleted: (id: string) => void;
  onRewrite: (page: string, force: boolean) => void;
}) {
  const state = useHubData<WikiPageDetail>(() => fetchWikiPage(project, page));
  const reload = state.reload;
  const pages = useMemo<PageLinks>(
    () => ({ known: knownPages ? new Set(knownPages) : null, href: (id) => hrefFor('debates', ['wiki', id]) }),
    [knownPages]
  );
  return (
    <DataGate state={state} what={tr.wiki.page.what(page)}>
      {(data) => (
        <PageBody
          project={project}
          data={data}
          pages={pages}
          busy={busy}
          onChanged={() => {
            reload();
            onChanged();
          }}
          onDeleted={onDeleted}
          onRewrite={onRewrite}
        />
      )}
    </DataGate>
  );
}

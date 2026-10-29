import { useEffect, useMemo, useState } from 'react';
import { StatusChip, TextField } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { Card } from '../../components/Card';
import { Disclosure } from '../../components/Disclosure';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { attentionPages, filterPages, wikiStateLabel, wikiStateTone } from '../../lib/wiki';
import type { WikiNode, WikiTree } from '../../lib/wiki';
import { hrefFor } from '../../router';
import type { WikiList, WikiPageSummary } from '../../types';

/** Id of the search box, so the `/` shortcut can focus it from anywhere in the wiki. */
export const WIKI_FIND_ID = 'wiki-find';

/** Scope entries shown on a card before "and N more". */
const SCOPE_SHOWN = 3;

export const wikiPageHref = (id?: string) => hrefFor('debates', id ? ['wiki', id] : ['wiki']);

function StateChip({ page }: { page: WikiPageSummary }) {
  return (
    <StatusChip tone={wikiStateTone(page.state)} title={page.state}>
      {wikiStateLabel(page.state)}
    </StatusChip>
  );
}

function KindChip({ page }: { page: WikiPageSummary }) {
  if (!page.kind || page.kind === 'dir') return null;
  return <StatusChip tone={page.kind === 'feature' ? 'info' : 'idle'}>{tr.wiki.kindLabel[page.kind] ?? page.kind}</StatusChip>;
}

const linkStyle = { color: 'var(--sg-text)', overflowWrap: 'anywhere', fontWeight: 600 } as const;

/** One page as a line: title link, then its chips and id. */
export function PageLine({ page, labelled = true }: { page: WikiPageSummary; labelled?: boolean }) {
  return (
    <Row gap="var(--sg-s-3)" style={{ flexWrap: 'wrap', minWidth: 0 }}>
      <a href={wikiPageHref(page.id)} aria-label={labelled ? tr.wiki.tab.openPage(page.id) : undefined} style={linkStyle}>
        {page.title}
      </a>
      <StateChip page={page} />
      <KindChip page={page} />
      <span style={{ ...mono, ...muted, fontSize: 12 }}>{page.id}</span>
    </Row>
  );
}

function ScopeLine({ scope }: { scope: string[] }) {
  if (scope.length === 0) return null;
  const shown = scope.slice(0, SCOPE_SHOWN);
  return (
    <div style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>
      {shown.join(', ')}
      {scope.length > shown.length ? ` ${tr.wiki.viewer.scopeMore(scope.length - shown.length)}` : ''}
    </div>
  );
}

/** A root page with what it is about and, collapsed, the group pages below it. */
function RootCard({ node }: { node: WikiNode }) {
  const { page, groups } = node;
  return (
    <Card tone={page.state === 'fresh' ? undefined : wikiStateTone(page.state)}>
      <Stack gap="var(--sg-s-3)">
        <PageLine page={page} />
        <ScopeLine scope={page.scope} />
        {page.summary ? <div style={{ fontSize: 13, overflowWrap: 'anywhere' }}>{page.summary}</div> : null}
        {groups.length > 0 ? (
          <Disclosure summary={tr.wiki.viewer.groupsCount(groups.length)}>
            <Stack gap="var(--sg-s-3)">
              {groups.map((g) => (
                <PageLine key={g.id} page={g} />
              ))}
            </Stack>
          </Disclosure>
        ) : null}
      </Stack>
    </Card>
  );
}

/**
 * The entry screen of the wiki: how much of it to trust, the pages to check first, the pages to start from and
 * the design documents. Typing in the search box swaps the sections for one flat list of matches.
 */
export function WikiIndex({ data, tree, focusFind }: { data: WikiList; tree: WikiTree; focusFind: boolean }) {
  const [find, setFind] = useState('');
  const searching = find.trim() !== '';
  const matches = useMemo(() => filterPages(data.pages, find), [data.pages, find]);
  const attention = useMemo(() => attentionPages(data.pages), [data.pages]);

  useEffect(() => {
    if (focusFind) document.getElementById(WIKI_FIND_ID)?.focus();
  }, [focusFind]);

  return (
    <Stack>
      {data.errors.map((e) => (
        <Notice key={e.id} tone="crit">
          {tr.wiki.tab.pageUnreadable(e.id, e.error)}
        </Notice>
      ))}
      {data.pages.length === 0 ? (
        <Card style={{ ...muted, fontSize: 13 }}>{tr.wiki.tab.empty}</Card>
      ) : (
        <>
          <Stack gap="var(--sg-s-3)">
            <div style={{ maxWidth: 480 }}>
              <TextField
                id={WIKI_FIND_ID}
                label={tr.wiki.viewer.searchLabel}
                placeholder={tr.wiki.viewer.searchPlaceholder}
                value={find}
                onChange={(e) => setFind(e.currentTarget.value)}
                type="search"
                autoComplete="off"
                spellCheck={false}
              />
            </div>
            <div style={{ ...muted, fontSize: 12 }}>
              {tr.wiki.viewer.summaryLine(data.pages.length, attention.length)}
              {' · '}
              {tr.wiki.tab.stateCheckedAgainst}
              <span style={mono} title={data.sha}>
                {data.sha.slice(0, 7)}
              </span>
              {tr.wiki.tab.stateCheckedSuffix}
            </div>
          </Stack>

          {searching ? (
            <Card>
              <Stack gap="var(--sg-s-4)">
                <strong>{tr.wiki.viewer.resultsTitle(matches.length)}</strong>
                {matches.length === 0 ? <div style={{ ...muted, fontSize: 13 }}>{tr.wiki.viewer.noMatch}</div> : null}
                {matches.map((p) => (
                  <PageLine key={p.id} page={p} />
                ))}
              </Stack>
            </Card>
          ) : (
            <>
              {attention.length > 0 ? (
                <section aria-label={tr.wiki.viewer.attentionTitle}>
                  <Notice tone={attention.some((p) => p.state === 'broken') ? 'crit' : 'warn'}>
                    <Stack gap="var(--sg-s-3)">
                      <strong>{tr.wiki.viewer.attentionTitle}</strong>
                      <div style={{ fontSize: 13 }}>{tr.wiki.viewer.attentionHint}</div>
                      {attention.map((p) => (
                        <PageLine key={p.id} page={p} labelled={false} />
                      ))}
                    </Stack>
                  </Notice>
                </section>
              ) : null}
              {tree.roots.length > 0 ? (
                <section aria-label={tr.wiki.viewer.startTitle}>
                  <Stack gap="var(--sg-s-4)">
                    <strong>{tr.wiki.viewer.startTitle}</strong>
                    {tree.roots.map((n) => (
                      <RootCard key={n.page.id} node={n} />
                    ))}
                  </Stack>
                </section>
              ) : null}
              {tree.features.length > 0 ? (
                <section aria-label={tr.wiki.viewer.featuresTitle}>
                  <Card>
                    <Stack gap="var(--sg-s-4)">
                      <strong>{tr.wiki.viewer.featuresTitle}</strong>
                      {tree.features.map((p) => (
                        <Stack key={p.id} gap="var(--sg-s-2)">
                          <PageLine page={p} />
                          {p.summary ? <div style={{ ...muted, fontSize: 13, overflowWrap: 'anywhere' }}>{p.summary}</div> : null}
                        </Stack>
                      ))}
                    </Stack>
                  </Card>
                </section>
              ) : null}
            </>
          )}
          <div style={{ ...muted, fontSize: 12 }}>{tr.wiki.viewer.shortcuts}</div>
        </>
      )}
    </Stack>
  );
}

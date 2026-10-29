import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { Card } from '../../components/Card';
import { Row, Stack, muted } from '../../components/layout';
import { pageNeighbours, parentOf, referencedBy, wikiStateLabel, wikiStateTone } from '../../lib/wiki';
import type { WikiTree } from '../../lib/wiki';
import type { WikiPageSummary } from '../../types';
import { wikiPageHref } from './WikiIndex';

const TREE_KEY = 'sg.wiki.tree.collapsed';
/** Anchor above the page, scrolled into view when the reader moves to another page. */
const TOP_ID = 'wiki-reading-top';

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(TREE_KEY) === '1';
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    window.localStorage.setItem(TREE_KEY, collapsed ? '1' : '0');
  } catch {
    // The page works without a remembered state.
  }
}

/** Where the reader is: `Index › parent › page` on wide screens, a way back and the parent on narrow ones. */
function PlaceBar({ page, current, parent, narrow }: { page: string; current: WikiPageSummary | undefined; parent: WikiPageSummary | null; narrow: boolean }) {
  const sep = <span aria-hidden="true" style={muted}> › </span>;
  if (narrow) {
    return (
      <nav aria-label={tr.wiki.viewer.placeAria}>
        <Row gap="var(--sg-s-4)" style={{ flexWrap: 'wrap' }}>
          <a href={wikiPageHref()} aria-label={tr.wiki.tab.backToPagesAria}>
            {tr.wiki.tab.backToPages}
          </a>
          {parent ? <a href={wikiPageHref(parent.id)}>{parent.title}</a> : null}
        </Row>
      </nav>
    );
  }
  return (
    <nav aria-label={tr.wiki.viewer.placeAria} style={{ fontSize: 13, overflowWrap: 'anywhere' }}>
      <a href={wikiPageHref()}>{tr.wiki.viewer.indexCrumb}</a>
      {parent ? (
        <>
          {sep}
          <a href={wikiPageHref(parent.id)}>{parent.title}</a>
        </>
      ) : null}
      {sep}
      <span aria-current="page">{current?.title ?? page}</span>
    </nav>
  );
}

function TreeLink({ page, selected, indent }: { page: WikiPageSummary; selected: boolean; indent: boolean }) {
  return (
    <a
      href={wikiPageHref(page.id)}
      aria-current={selected ? 'page' : undefined}
      aria-label={tr.wiki.tab.openPage(page.id)}
      style={{
        display: 'flex',
        gap: 'var(--sg-s-3)',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginLeft: indent ? 'var(--sg-s-6)' : undefined,
        padding: 'var(--sg-s-2) var(--sg-s-4)',
        borderRadius: 'var(--sg-r-md)',
        background: selected ? 'var(--sg-surface-selected)' : undefined,
        color: 'var(--sg-text)',
        fontSize: 13,
        fontWeight: selected ? 600 : undefined,
        textDecoration: 'none',
        overflowWrap: 'anywhere',
      }}
    >
      <span>{page.title}</span>
      {page.state !== 'fresh' ? (
        <StatusChip tone={wikiStateTone(page.state)} title={page.state}>
          {wikiStateLabel(page.state)}
        </StatusChip>
      ) : null}
    </a>
  );
}

/** The index tree, narrow and collapsible, so the reader can jump without going back to the index. */
function PageTree({ tree, page }: { tree: WikiTree; page: string }) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggle = () => {
    writeCollapsed(!collapsed);
    setCollapsed(!collapsed);
  };
  return (
    <div style={{ flex: collapsed ? '0 0 auto' : '1 1 220px', maxWidth: collapsed ? undefined : 300, minWidth: 0, position: 'sticky', top: 0 }}>
      <Stack gap="var(--sg-s-3)">
        <Button size="sm" variant="ghost" aria-expanded={!collapsed} onClick={toggle}>
          {collapsed ? tr.wiki.viewer.treeExpand : tr.wiki.viewer.treeCollapse}
        </Button>
        {collapsed ? null : (
          <nav aria-label={tr.wiki.tab.pagesAria} style={{ maxHeight: 'min(70vh, 640px)', overflowY: 'auto' }}>
            {tree.roots.map((n) => (
              <div key={n.page.id}>
                <TreeLink page={n.page} selected={n.page.id === page} indent={false} />
                {n.groups.map((g) => (
                  <TreeLink key={g.id} page={g} selected={g.id === page} indent />
                ))}
              </div>
            ))}
            {tree.features.map((f) => (
              <TreeLink key={f.id} page={f} selected={f.id === page} indent={false} />
            ))}
          </nav>
        )}
      </Stack>
    </div>
  );
}

function Neighbour({ page, dir }: { page: WikiPageSummary | null; dir: 'prev' | 'next' }) {
  if (!page) return <span />;
  const prev = dir === 'prev';
  return (
    <a
      href={wikiPageHref(page.id)}
      rel={prev ? 'prev' : 'next'}
      aria-label={prev ? tr.wiki.viewer.previousAria(page.title) : tr.wiki.viewer.nextAria(page.title)}
      style={{ display: 'grid', gap: 'var(--sg-s-1)', textAlign: prev ? 'left' : 'right', maxWidth: '48%', overflowWrap: 'anywhere' }}
    >
      <span style={{ ...muted, fontSize: 12 }}>{prev ? `‹ ${tr.wiki.viewer.previous}` : `${tr.wiki.viewer.next} ›`}</span>
      <span>{page.title}</span>
    </a>
  );
}

function PageList({ title, pages }: { title: string; pages: WikiPageSummary[] }) {
  if (pages.length === 0) return null;
  return (
    <Stack gap="var(--sg-s-3)">
      <strong style={{ fontSize: 13 }}>{title}</strong>
      <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', display: 'grid', gap: 'var(--sg-s-2)', fontSize: 13 }}>
        {pages.map((p) => (
          <li key={p.id}>
            <a href={wikiPageHref(p.id)} aria-label={tr.wiki.tab.openPage(p.id)}>
              {p.title}
            </a>
          </li>
        ))}
      </ul>
    </Stack>
  );
}

/**
 * The reading view around one page: the place bar above it, and below it the previous and next page in reading
 * order, the groups of an overview and the pages that link here. On wide screens the index tree stands to the left.
 */
export function WikiReading({
  page,
  pages,
  tree,
  order,
  narrow,
  children,
}: {
  page: string;
  pages: readonly WikiPageSummary[];
  tree: WikiTree;
  order: readonly WikiPageSummary[];
  narrow: boolean;
  children: ReactNode;
}) {
  const current = pages.find((p) => p.id === page);
  const { prev, next } = pageNeighbours(order, page);
  const groups = tree.roots.find((n) => n.page.id === page)?.groups ?? [];
  const cited = referencedBy(pages, page);

  // Moving to another page starts at its top. This also replaces the position the browser would restore on Back.
  useEffect(() => {
    document.getElementById(TOP_ID)?.scrollIntoView?.({ block: 'start' });
  }, [page]);

  const main = (
    <div style={{ flex: '4 1 520px', minWidth: 0 }}>
      <Stack>
        <div id={TOP_ID}>
          <PlaceBar page={page} current={current} parent={parentOf(tree, page)} narrow={narrow} />
        </div>
        {children}
        {prev || next || groups.length > 0 || cited.length > 0 ? (
          <Card>
            <Stack gap="var(--sg-s-6)">
              <PageList title={tr.wiki.viewer.groupsOf(groups.length)} pages={groups} />
              <PageList title={tr.wiki.viewer.referencedBy} pages={cited} />
              {prev || next ? (
                <nav aria-label={tr.wiki.viewer.neighboursAria} style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--sg-s-6)' }}>
                  <Neighbour page={prev} dir="prev" />
                  <Neighbour page={next} dir="next" />
                </nav>
              ) : null}
            </Stack>
          </Card>
        ) : null}
        {narrow ? null : <div style={{ ...muted, fontSize: 12 }}>{tr.wiki.viewer.shortcuts}</div>}
      </Stack>
    </div>
  );

  if (narrow) return main;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-8)', alignItems: 'flex-start' }}>
      <PageTree tree={tree} page={page} />
      {main}
    </div>
  );
}

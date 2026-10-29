import { useMemo } from 'react';
import { SegmentedControl } from '@server-guardian/ui';
import { fetchWikiPages } from '../../api';
import { tr } from '../../i18n';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { ProjectSwitcher } from '../../components/ProjectSwitcher';
import { Row, Stack, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { useNarrow } from '../../hooks/useNarrow';
import { useWikiBuild } from '../../hooks/useWikiBuild';
import { useWikiShortcuts } from '../../hooks/useWikiShortcuts';
import { FILES_HREF, buildWikiTree, indexOrder, pageNeighbours, wikiMode } from '../../lib/wiki';
import { hrefFor, navigate, useRoute } from '../../router';
import { useDebateStore } from '../../state/debateStore';
import { useProjectStore } from '../../state/projectStore';
import type { WikiList } from '../../types';
import { FileList } from './FileList';
import { FileView } from './FileView';
import { WikiHistory } from './WikiHistory';
import { WIKI_FIND_ID, WikiIndex } from './WikiIndex';
import { WikiPageView } from './WikiPageView';
import { WikiReading } from './WikiReading';

/**
 * The wiki of one project in three modes: reading pages (the index screen and the page being read), the repository files
 * (`?view=files`, `?file=<path>[&lines=N-M|&symbol=S]`, no LLM) and the build history with the actions that start a build
 * (`?view=history`). Only the history mode carries build buttons; reading keeps the page-actions menu.
 */
function WikiProject({ project, page }: { project: string; page?: string }) {
  const narrow = useNarrow();
  const { query } = useRoute();
  const mode = wikiMode(page, query);
  const file = mode === 'files' ? query.file || undefined : undefined;
  const { recent } = useDebateStore();
  const list = useHubData<WikiList>(() => fetchWikiPages(project));
  const reloadList = list.reload;
  const { build, building, busy, banner } = useWikiBuild(project, reloadList);

  const pages = list.data?.pages;
  const tree = useMemo(() => buildWikiTree(pages ?? []), [pages]);
  const order = useMemo(() => indexOrder(tree), [tree]);
  const knownPages = useMemo(() => (pages ? pages.map((p) => p.id) : null), [pages]);

  const atIndex = mode === 'read' && !page;
  const { prev, next } = page ? pageNeighbours(order, page) : { prev: null, next: null };
  useWikiShortcuts(
    mode !== 'read'
      ? {}
      : {
          previous: prev ? () => navigate('debates', ['wiki', prev.id]) : undefined,
          next: next ? () => navigate('debates', ['wiki', next.id]) : undefined,
          index: atIndex ? undefined : () => navigate('debates', ['wiki']),
          find: () => (atIndex ? document.getElementById(WIKI_FIND_ID)?.focus() : navigate('debates', ['wiki'], { find: '1' })),
        }
  );

  // On a narrow screen an open page or file takes the whole width, with a way back instead of the header.
  const opened = mode === 'files' ? !!file : !!page;
  const showHeader = !narrow || !opened;
  const showList = !narrow || !opened;
  const showDetail = !narrow || opened;

  return (
    <Stack>
      {narrow && mode === 'files' && opened ? (
        <div>
          <a href={FILES_HREF}>{tr.wiki.files.backToFiles}</a>
        </div>
      ) : null}
      {showHeader ? (
        <Card>
          <Row style={{ alignItems: 'flex-end', justifyContent: 'space-between' }}>
            <ProjectSwitcher />
            <SegmentedControl
              aria-label={tr.wiki.modes.aria}
              options={[
                { value: 'read', label: tr.wiki.modes.read },
                { value: 'files', label: tr.wiki.modes.files },
                { value: 'history', label: tr.wiki.modes.history },
              ]}
              value={mode}
              onChange={(v) => navigate('debates', ['wiki'], v === 'read' ? {} : { view: v })}
            />
          </Row>
        </Card>
      ) : null}

      {banner ? (
        <Notice tone={banner.tone}>
          {banner.text}
          {banner.job ? (
            <>
              {' '}
              <a href={hrefFor('debates', [banner.job])}>{tr.wiki.tab.followInMonitor}</a>
            </>
          ) : null}
        </Notice>
      ) : null}
      {building && !banner?.job ? (
        <Notice tone="ok">
          {tr.wiki.tab.buildRunning} <a href={hrefFor('debates', [building.id])}>{tr.wiki.tab.followInMonitor}</a>
        </Notice>
      ) : null}

      {mode === 'files' ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-8)', alignItems: 'flex-start' }}>
          {showList ? (
            <div style={{ flex: opened ? '1 1 240px' : '1 1 300px', minWidth: 0 }}>
              <FileList project={project} selected={file} />
            </div>
          ) : null}
          {showDetail ? (
            <div style={{ flex: '4 1 520px', minWidth: 0 }}>
              {file ? (
                <FileView key={file} project={project} path={file} lines={query.lines} symbol={query.symbol} />
              ) : (
                <Card style={{ textAlign: 'center', ...muted }}>{tr.wiki.files.selectFile}</Card>
              )}
            </div>
          ) : null}
        </div>
      ) : mode === 'history' ? (
        <WikiHistory project={project} list={list} jobs={recent.data} busy={busy} onBuild={(req) => void build(req)} />
      ) : page ? (
        <WikiReading page={page} pages={pages ?? []} tree={tree} order={order} narrow={narrow}>
          <WikiPageView
            key={page}
            project={project}
            page={page}
            knownPages={knownPages}
            busy={busy}
            onChanged={reloadList}
            onDeleted={() => {
              reloadList();
              navigate('debates', ['wiki']);
            }}
            onRewrite={(id, force) => void build(force ? { pages: [id], force: true } : { pages: [id] })}
          />
        </WikiReading>
      ) : (
        <DataGate state={list} what={tr.wiki.tab.pagesWhat}>
          {(data) => <WikiIndex data={data} tree={tree} focusFind={query.find === '1'} />}
        </DataGate>
      )}
    </Stack>
  );
}

/** Wiki view of the debates page (`#/debates/wiki[/:page]`) for the selected project. */
export function WikiTab({ page }: { page?: string }) {
  const { selected } = useProjectStore();
  // Keyed by project so a switch starts from that project's own list.
  return <WikiProject key={selected} project={selected} page={page} />;
}

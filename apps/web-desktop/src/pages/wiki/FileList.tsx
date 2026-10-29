import { useMemo, useState } from 'react';
import { Panel, TextField } from '@server-guardian/ui';
import { fetchCodeFiles } from '../../api';
import { tr } from '../../i18n';
import { DataGate } from '../../components/DataGate';
import { Notice } from '../../components/Notice';
import { Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { fileHref } from '../../lib/wiki';
import type { CodeFileList } from '../../types';

/** Rows shown at once; a narrower search shows the rest. */
export const FILE_LIST_LIMIT = 300;

/** Paths that contain every whitespace-separated term of the query (case-insensitive), in path order. */
export function filterPaths(paths: string[], query: string): string[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return paths;
  return paths.filter((p) => {
    const lower = p.toLowerCase();
    return terms.every((t) => lower.includes(t));
  });
}

function FileRow({ path, selected }: { path: string; selected: boolean }) {
  const slash = path.lastIndexOf('/');
  return (
    <a
      href={fileHref(path)}
      aria-current={selected ? 'true' : undefined}
      aria-label={tr.wiki.files.openFile(path)}
      title={path}
      style={{
        display: 'block',
        padding: '3px var(--sg-s-4)',
        borderRadius: 'var(--sg-r-md)',
        background: selected ? 'var(--sg-surface-selected)' : undefined,
        color: 'var(--sg-text)',
        textDecoration: 'none',
        fontSize: 12,
        ...mono,
        overflowWrap: 'anywhere',
      }}
    >
      {slash >= 0 ? <span style={muted}>{path.slice(0, slash + 1)}</span> : null}
      {path.slice(slash + 1)}
    </a>
  );
}

/** Searchable list of the readable files of the project's commit. */
export function FileList({ project, selected }: { project: string; selected?: string }) {
  const list = useHubData<CodeFileList>(() => fetchCodeFiles(project));
  const [query, setQuery] = useState('');
  const paths = useMemo(() => (list.data ? list.data.files.map((f) => f.path) : []), [list.data]);
  const matched = useMemo(() => filterPaths(paths, query), [paths, query]);

  return (
    <Panel title={tr.wiki.files.listTitle(list.data ? list.data.files.length : null)}>
      <DataGate state={list} what={tr.wiki.files.listWhat}>
        {(data) => (
          <Stack gap="var(--sg-s-4)">
            <TextField
              label={tr.wiki.files.search}
              placeholder={tr.wiki.files.searchPlaceholder}
              value={query}
              onChange={(e) => setQuery(e.currentTarget.value)}
              mono
              type="search"
              autoComplete="off"
              spellCheck={false}
            />
            {data.truncated ? <Notice tone="warn">{tr.wiki.files.truncated}</Notice> : null}
            {matched.length === 0 ? (
              <div style={{ ...muted, fontSize: 13 }}>{tr.wiki.files.noMatch}</div>
            ) : (
              <nav aria-label={tr.wiki.files.listAria} style={{ maxHeight: 'min(70vh, 640px)', overflowY: 'auto' }}>
                {matched.slice(0, FILE_LIST_LIMIT).map((p) => (
                  <FileRow key={p} path={p} selected={p === selected} />
                ))}
              </nav>
            )}
            {matched.length > FILE_LIST_LIMIT ? <div style={{ ...muted, fontSize: 12 }}>{tr.wiki.files.more(matched.length - FILE_LIST_LIMIT)}</div> : null}
          </Stack>
        )}
      </DataGate>
    </Panel>
  );
}

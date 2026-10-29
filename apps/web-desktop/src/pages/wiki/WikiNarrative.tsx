import { useCallback, useState } from 'react';
import { Button } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { Caption } from '../../components/Card';
import { MarkdownDoc } from '../../components/MarkdownDoc';
import { Row } from '../../components/layout';
import { fileHref } from '../../lib/wiki';
import { decorateNarrative, type Citation, type PageLinks, type WikiHeading } from '../../lib/wikiCitations';

const CITES_KEY = 'sg.wiki.showCitations';

function readShowCitations(): boolean {
  try {
    return window.localStorage.getItem(CITES_KEY) !== '0';
  } catch {
    return true;
  }
}

function writeShowCitations(show: boolean): void {
  try {
    window.localStorage.setItem(CITES_KEY, show ? '1' : '0');
  } catch {
    // Storage may be unavailable; the toggle still works for this view.
  }
}

/** A citation chip opens the file view at the cited lines or declaration. */
function citationHref(c: Citation): string {
  return c.location.startsWith('#') ? fileHref(c.path, { symbol: c.location.slice(1) }) : fileHref(c.path, { lines: c.location.slice(1) });
}

const LAYOUT_CSS = `
.sg-wiki-read { container-type: inline-size; }
.sg-wiki-grid { display: grid; grid-template-columns: minmax(0, 78ch); gap: var(--sg-s-6); align-items: start; }
.sg-wiki-toc { display: none; }
@container (min-width: 720px) {
  .sg-wiki-grid { grid-template-columns: minmax(0, 78ch) minmax(140px, 200px); }
  .sg-wiki-toc { display: block; position: sticky; top: var(--sg-s-4); font-size: 13px; border-left: 1px solid var(--sg-border); padding-left: var(--sg-s-4); }
}
.sg-wiki-read a.sg-wikilink { font-family: var(--sg-font-mono); font-size: 12px; color: var(--sg-accent); text-decoration: none; background: var(--sg-code-bg); border: 1px solid var(--sg-border); border-radius: 4px; padding: 0 5px; }
.sg-wiki-read a.sg-wikilink:hover, .sg-wiki-read a.sg-wikilink:focus-visible { border-color: var(--sg-info-border); text-decoration: underline; }
.sg-wiki-read .sg-wikilink-missing { font-family: var(--sg-font-mono); font-size: 12px; color: var(--sg-text-secondary); border: 1px dashed var(--sg-border); border-radius: 4px; padding: 0 5px; }
.sg-wiki-toc ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.sg-wiki-toc button { all: unset; cursor: pointer; color: var(--sg-text-secondary); overflow-wrap: anywhere; }
.sg-wiki-toc button:hover, .sg-wiki-toc button:focus-visible { color: var(--sg-text); text-decoration: underline; }
.sg-wiki-toc .sg-wiki-toc-h3 { padding-left: var(--sg-s-4); }
`;

/**
 * The written layer of a wiki page, rendered as Markdown for reading: citations become chips (the trailing ones
 * can be hidden), the body is limited to a readable line length, and wide views get a table of contents.
 */
export function WikiNarrative({
  pageId,
  title,
  author,
  markdown,
  pages,
}: {
  pageId: string;
  title: string;
  author: string;
  markdown: string;
  /** Where `[[page-id]]` links go; omitted, they stay text. */
  pages?: PageLinks;
}) {
  const [headings, setHeadings] = useState<WikiHeading[]>([]);
  const [citations, setCitations] = useState(0);
  const [showCitations, setShowCitations] = useState(readShowCitations);

  const onRendered = useCallback(
    (root: HTMLElement) => {
      const found = decorateNarrative(root, title, `wiki-${pageId}`, citationHref, pages);
      setHeadings(found.headings);
      setCitations(found.citations);
    },
    [pageId, title, pages]
  );

  const toggle = () => {
    setShowCitations((v) => {
      writeShowCitations(!v);
      return !v;
    });
  };

  return (
    <div className="sg-wiki-read" style={{ display: 'grid', gap: 'var(--sg-s-3)' }}>
      <style>{LAYOUT_CSS}</style>
      <Row style={{ justifyContent: 'space-between' }}>
        <Caption>{tr.wiki.page.narrative(author)}</Caption>
        {citations > 0 ? (
          <Button size="sm" variant="outline" aria-pressed={showCitations} onClick={toggle}>
            {showCitations ? tr.wiki.page.hideCitations : tr.wiki.page.showCitations(citations)}
          </Button>
        ) : null}
      </Row>
      <div className="sg-wiki-grid">
        <article className={showCitations ? undefined : 'sg-cites-off'} style={{ minWidth: 0 }}>
          <MarkdownDoc markdown={markdown} onRendered={onRendered} />
        </article>
        {headings.length > 1 ? (
          <nav className="sg-wiki-toc" aria-label={tr.wiki.page.toc}>
            <ul>
              {headings.map((h) => (
                <li key={h.id} className={h.level === 3 ? 'sg-wiki-toc-h3' : undefined}>
                  <button type="button" onClick={() => document.getElementById(h.id)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })}>
                    {h.text}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
        ) : null}
      </div>
    </div>
  );
}

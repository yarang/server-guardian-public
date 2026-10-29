/**
 * Reading aids for a rendered wiki narrative (DOM passes over the sanitized MarkdownDoc output).
 *
 * The narrative is written for agents: nearly every sentence ends in a citation such as
 * `(cli/llm-agent/debate_engine.py:712-713)`. For people these become small chips that show the file name and
 * location, with the full citation as the tooltip. A parenthesised run of citations becomes an "aside" group the
 * reader can hide; a citation that is part of the sentence stays visible. Code blocks are never touched.
 */

/** A strong citation: a path with an extension, then `:N`, `:N-M` or `#Symbol` (the forms the wiki writer must use). */
const CITE_SRC = String.raw`((?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]*[A-Za-z0-9_]\.[A-Za-z][A-Za-z0-9]{0,7})(?::(\d+)(?:-(\d+))?|#([A-Za-z_$][\w$.]*))`;
const CITE_FULL = new RegExp(`^${CITE_SRC}$`);
/** In running text: not preceded by a path character, so `foo/bar.py:3` is not matched from `bar.py`. */
const CITE_IN_TEXT = () => new RegExp(`(^|[^\\w./-])${CITE_SRC}`, 'g');
const SEPARATOR = /^[\s,;]*$/;

export interface Citation {
  path: string;
  /** `:12`, `:12-14` or `#Symbol`. */
  location: string;
}

/** Parses one whole citation (`path:N`, `path:N-M`, `path#Symbol`), or null. */
export function parseCitation(text: string): Citation | null {
  const m = CITE_FULL.exec(text.trim());
  if (!m) return null;
  const location = m[4] !== undefined ? `#${m[4]}` : `:${m[2]}${m[3] !== undefined ? `-${m[3]}` : ''}`;
  return { path: m[1], location };
}

/** Href a chip links to (the file view), or none. */
export type CitationLink = (c: Citation) => string | null;

function chip(doc: Document, c: Citation, link?: CitationLink): HTMLElement {
  const href = link?.(c) ?? null;
  const el = doc.createElement(href ? 'a' : 'span');
  if (href) el.setAttribute('href', href);
  el.className = 'sg-cite';
  const full = `${c.path}${c.location}`;
  el.title = full;
  el.dataset.cite = full;
  el.textContent = `${c.path.slice(c.path.lastIndexOf('/') + 1)}${c.location}`;
  return el;
}

const isChip = (n: Node | null): n is HTMLElement => n instanceof HTMLElement && n.classList.contains('sg-cite');
const isSeparator = (n: Node | null): n is Text => n !== null && n.nodeType === Node.TEXT_NODE && SEPARATOR.test(n.textContent ?? '');
const insideCode = (n: Node): boolean => !!n.parentElement?.closest('pre, code, .sg-cite');

/** Splits citations out of the text nodes of `root` (outside code) and turns citation-only inline code into chips. */
function makeChips(root: HTMLElement, link?: CitationLink): number {
  const doc = root.ownerDocument;
  let count = 0;
  for (const code of Array.from(root.querySelectorAll('code'))) {
    if (code.closest('pre')) continue;
    const c = parseCitation(code.textContent ?? '');
    if (!c) continue;
    code.replaceWith(chip(doc, c, link));
    count += 1;
  }
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) if (!insideCode(n)) texts.push(n as Text);
  for (const node of texts) {
    const text = node.textContent ?? '';
    const re = CITE_IN_TEXT();
    let last = 0;
    const parts: Node[] = [];
    for (let m = re.exec(text); m; m = re.exec(text)) {
      const start = m.index + m[1].length;
      const c = parseCitation(m[0].slice(m[1].length));
      if (!c) continue;
      if (start > last) parts.push(doc.createTextNode(text.slice(last, start)));
      parts.push(chip(doc, c, link));
      count += 1;
      last = start + m[0].length - m[1].length;
    }
    if (parts.length === 0) continue;
    if (last < text.length) parts.push(doc.createTextNode(text.slice(last)));
    node.replaceWith(...parts);
  }
  return count;
}

/**
 * Wraps each run of chips (siblings separated only by spaces, commas or semicolons) in a `.sg-cite-group`.
 * A run that is the whole content of a parenthesis loses the parentheses (and the space before them) and is
 * marked `.sg-cite-aside`: that is the citation trailing a claim, which the reader may hide.
 */
function groupChips(root: HTMLElement): void {
  const doc = root.ownerDocument;
  for (const first of Array.from(root.querySelectorAll('.sg-cite'))) {
    if (first.parentElement?.classList.contains('sg-cite-group')) continue;
    const run: Node[] = [first];
    let tail: Node = first;
    for (let n = tail.nextSibling; n; n = n.nextSibling) {
      if (isChip(n)) {
        run.push(n);
        tail = n;
      } else if (!isSeparator(n)) break;
    }
    // Separators between the chips belong to the run; trailing separators do not.
    const members: Node[] = [];
    for (let n: Node | null = first; n; n = n.nextSibling) {
      members.push(n);
      if (n === tail) break;
    }
    const before = first.previousSibling;
    const after = tail.nextSibling;
    const opens = before?.nodeType === Node.TEXT_NODE && /\(\s*$/.test(before.textContent ?? '');
    const closes = after?.nodeType === Node.TEXT_NODE && /^\s*\)/.test(after.textContent ?? '');
    const group = doc.createElement('span');
    group.className = opens && closes ? 'sg-cite-group sg-cite-aside' : 'sg-cite-group';
    first.before(group);
    group.append(...members);
    if (opens && closes && before && after) {
      before.textContent = (before.textContent ?? '').replace(/\s*\(\s*$/, '');
      after.textContent = (after.textContent ?? '').replace(/^\s*\)/, '');
    }
  }
}

/** How `[[page-id]]` links (the writer's form for related pages) become links to other wiki pages. */
export interface PageLinks {
  /** Ids of the pages that exist; an id outside it is shown as a missing page. `null` links every id. */
  known: ReadonlySet<string> | null;
  href: (id: string) => string;
}

const WIKILINK_SRC = String.raw`\[\[([a-z0-9][a-z0-9-]{0,63})\]\]`;
const WIKILINK_FULL = new RegExp(`^${WIKILINK_SRC}$`);

function pageLink(doc: Document, id: string, links: PageLinks): HTMLElement {
  const missing = links.known !== null && !links.known.has(id);
  const el = doc.createElement(missing ? 'span' : 'a');
  if (!missing) el.setAttribute('href', links.href(id));
  el.className = missing ? 'sg-wikilink sg-wikilink-missing' : 'sg-wikilink';
  el.dataset.page = id;
  el.textContent = id;
  return el;
}

/** Turns `[[id]]` in running text, and inline code that is only `[[id]]`, into page links. Returns how many. */
function makePageLinks(root: HTMLElement, links: PageLinks): number {
  const doc = root.ownerDocument;
  let count = 0;
  for (const code of Array.from(root.querySelectorAll('code'))) {
    if (code.closest('pre')) continue;
    const m = WIKILINK_FULL.exec((code.textContent ?? '').trim());
    if (!m) continue;
    code.replaceWith(pageLink(doc, m[1], links));
    count += 1;
  }
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) if (!n.parentElement?.closest('pre, code, a')) texts.push(n as Text);
  for (const node of texts) {
    const text = node.textContent ?? '';
    const re = new RegExp(WIKILINK_SRC, 'g');
    let last = 0;
    const parts: Node[] = [];
    for (let m = re.exec(text); m; m = re.exec(text)) {
      if (m.index > last) parts.push(doc.createTextNode(text.slice(last, m.index)));
      parts.push(pageLink(doc, m[1], links));
      count += 1;
      last = m.index + m[0].length;
    }
    if (parts.length === 0) continue;
    if (last < text.length) parts.push(doc.createTextNode(text.slice(last)));
    node.replaceWith(...parts);
  }
  return count;
}

export interface WikiHeading {
  id: string;
  text: string;
  level: 2 | 3;
}

/**
 * Prepares a rendered narrative for reading: drops a leading `# title` that repeats the page title,
 * turns citations into chips (links when `link` gives an href), turns `[[page-id]]` into links to the other pages
 * (when `pages` is given), and gives the h2/h3 headings ids for the table of contents. Returns the headings and the
 * number of citations found.
 */
export function decorateNarrative(
  root: HTMLElement,
  title: string,
  idPrefix: string,
  link?: CitationLink,
  pages?: PageLinks
): { headings: WikiHeading[]; citations: number } {
  const h1 = root.firstElementChild;
  if (h1?.tagName === 'H1' && (h1.textContent ?? '').trim() === title.trim()) h1.remove();
  if (pages) makePageLinks(root, pages);
  const citations = makeChips(root, link);
  groupChips(root);
  const headings: WikiHeading[] = [];
  root.querySelectorAll('h2, h3').forEach((h, i) => {
    h.id = `${idPrefix}-h${i}`;
    headings.push({ id: h.id, text: (h.textContent ?? '').trim(), level: h.tagName === 'H2' ? 2 : 3 });
  });
  return { headings, citations };
}

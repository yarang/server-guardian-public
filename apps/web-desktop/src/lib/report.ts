import DOMPurify from 'dompurify';
import { Marked } from 'marked';
import type { Tokens } from 'marked';

/**
 * Thread result document: Markdown to safe HTML, plus the self-contained export.
 *
 * Everything in the Markdown is untrusted (agent transcripts and an LLM-written diagram), so:
 *  - raw HTML in the Markdown is escaped, never passed through;
 *  - only http(s) links survive and they always carry rel="noopener noreferrer" target="_blank";
 *  - images other than the thread's own `<thread>.assets/<name>.svg` become their alt text;
 *  - the rendered HTML and every SVG go through DOMPurify before reaching the DOM.
 */

const ASSET_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}\.svg$/;
const ASSET_LINK_RE_SRC = '!\\[([^\\]]*)\\]\\(([A-Za-z0-9_]+)\\.assets/([A-Za-z0-9_][A-Za-z0-9_.-]{0,100}\\.svg)\\)';

const htmlPurify = DOMPurify(window);
htmlPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('rel', 'noopener noreferrer');
    node.setAttribute('target', '_blank');
  }
});

export interface RenderedDoc {
  /** Sanitized HTML. `[data-asset]` and `[data-mermaid]` spans/divs are placeholders to fill in. */
  html: string;
  /** Asset file names referenced by the document, in order of appearance, unique. */
  assets: string[];
  /** Source of every ```mermaid block; `data-mermaid` is the index into this list. */
  diagrams: string[];
}

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** Parses the asset file name out of an `![..](<thread>.assets/<name>.svg)` href, or null for any other href. */
export function assetNameOf(href: string, thread: string): string | null {
  if (!thread) return null;
  const prefix = `${thread}.assets/`;
  if (!href.startsWith(prefix)) return null;
  const name = href.slice(prefix.length);
  return ASSET_NAME_RE.test(name) ? name : null;
}

/**
 * `topHeading` (2-6) demotes the document's headings so the shallowest one becomes that level. Agent answers
 * bring their own `#` outline; shown inside a card they would outrank the surrounding page headings.
 */
export function renderReport(markdown: string, thread: string, topHeading = 0): RenderedDoc {
  const assets: string[] = [];
  const diagrams: string[] = [];
  const md = new Marked({
    gfm: true,
    renderer: {
      html: ({ text, block }: Tokens.HTML | Tokens.Tag) => (block ? `<p>${esc(text.trim())}</p>\n` : esc(text)),
      code({ text, lang }: Tokens.Code) {
        if ((lang || '').trim().split(/\s+/)[0].toLowerCase() === 'mermaid') {
          diagrams.push(text);
          return `<div class="sg-md-mermaid" data-mermaid="${diagrams.length - 1}"></div>`;
        }
        return `<pre><code>${esc(text)}</code></pre>\n`;
      },
      image({ href, text }: Tokens.Image) {
        const name = assetNameOf(href, thread);
        if (!name) return esc(text);
        if (!assets.includes(name)) assets.push(name);
        return `<span class="sg-md-asset" role="img" aria-label="${esc(text)}" data-asset="${esc(name)}"></span>`;
      },
      link(this: { parser: { parseInline(t: Tokens.Generic[]): string } }, { href, tokens, raw }: Tokens.Link) {
        // Anything but an http(s) link is shown as written: agent text like `key[1](role)` is not a link.
        if (!/^https?:\/\//i.test(href)) return esc(raw);
        return `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${this.parser.parseInline(tokens)}</a>`;
      },
    },
  });
  const tokens = md.lexer(markdown);
  if (topHeading > 0) {
    let shallowest = 7;
    md.walkTokens(tokens, (t) => {
      if (t.type === 'heading') shallowest = Math.min(shallowest, (t as Tokens.Heading).depth);
    });
    const shift = shallowest <= 6 ? topHeading - shallowest : 0;
    if (shift > 0) {
      md.walkTokens(tokens, (t) => {
        if (t.type === 'heading') (t as Tokens.Heading).depth = Math.min(6, (t as Tokens.Heading).depth + shift);
      });
    }
  }
  const raw = md.parser(tokens);
  const html = htmlPurify.sanitize(raw, { ADD_ATTR: ['target'], RETURN_TRUSTED_TYPE: false }) as string;
  return { html, assets, diagrams };
}

/** Sanitizes SVG text with DOMPurify's SVG profile; the result is safe to inline. */
export function sanitizeSvg(svg: string): string {
  return DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    ADD_TAGS: ['foreignObject'],
    RETURN_TRUSTED_TYPE: false,
  }) as string;
}

function toBase64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

/** `data:image/svg+xml;base64,...` of the (sanitized) SVG text. */
export function svgDataUri(svg: string): string {
  return `data:image/svg+xml;base64,${toBase64(sanitizeSvg(svg))}`;
}

/**
 * Self-contained Markdown: every `![alt](<thread>.assets/<name>.svg)` becomes an inline data URI.
 * Mermaid blocks stay as code. A failing asset fails the whole export (no silent broken images).
 */
export async function buildStandaloneMarkdown(markdown: string, thread: string, loadSvg: (name: string) => Promise<string>): Promise<string> {
  const re = new RegExp(ASSET_LINK_RE_SRC, 'g');
  const names = new Set<string>();
  for (const m of markdown.matchAll(re)) if (m[2] === thread) names.add(m[3]);
  const uris = new Map<string, string>();
  await Promise.all(Array.from(names).map(async (n) => uris.set(n, svgDataUri(await loadSvg(n)))));
  return markdown.replace(new RegExp(ASSET_LINK_RE_SRC, 'g'), (whole, alt: string, t: string, name: string) => {
    const uri = t === thread ? uris.get(name) : undefined;
    return uri ? `![${alt}](${uri})` : whole;
  });
}

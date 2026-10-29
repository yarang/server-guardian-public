import { useEffect, useMemo, useRef, useState } from 'react';
import { tr } from '../i18n';
import { errorText } from '../lib/format';
import { renderReport, sanitizeSvg } from '../lib/report';
import { Notice } from './Notice';

type MermaidApi = typeof import('mermaid').default;

let mermaidSeq = 0;

const noAssets = (name: string): Promise<string> => Promise.reject(new Error(`no asset source for ${name}`));

/** The app only ships the dark graphite theme; honour an explicit light colour scheme should one appear. */
function mermaidTheme(): 'dark' | 'default' {
  return document.documentElement.style.colorScheme === 'light' ? 'default' : 'dark';
}

const SCOPED_CSS = `
.sg-md { color: var(--sg-text); font-size: 14px; line-height: 1.65; overflow-wrap: anywhere; min-width: 0; }
.sg-md > :first-child { margin-top: 0; }
.sg-md h1, .sg-md h2, .sg-md h3, .sg-md h4, .sg-md h5, .sg-md h6 { color: var(--sg-text); line-height: 1.3; margin: 1.6em 0 0.5em; }
.sg-md h1 { font-size: 24px; } .sg-md h2 { font-size: 20px; padding-bottom: 6px; border-bottom: 1px solid var(--sg-border); }
.sg-md h3 { font-size: 16px; } .sg-md h4 { font-size: 14px; color: var(--sg-text); }
.sg-md h5, .sg-md h6 { font-size: 13px; color: var(--sg-text-secondary); }
.sg-md strong { color: var(--sg-text); }
.sg-md p, .sg-md ul, .sg-md ol, .sg-md blockquote, .sg-md pre, .sg-md .sg-md-scroll { margin: 0 0 0.9em; }
.sg-md ul, .sg-md ol { padding-left: 1.6em; }
.sg-md a { color: var(--sg-accent); }
.sg-md hr { border: 0; border-top: 1px solid var(--sg-border); margin: 2em 0 0; }
.sg-md hr + h3 { margin-top: 1.2em; }
.sg-md code { font-family: var(--sg-font-mono); font-size: 12px; background: var(--sg-code-bg); border: 1px solid var(--sg-border); border-radius: 4px; padding: 1px 5px; }
.sg-md pre { background: var(--sg-code-bg); border: 1px solid var(--sg-border); border-radius: var(--sg-r-md); padding: var(--sg-s-4) var(--sg-s-5); overflow: auto; max-width: 100%; box-sizing: border-box; }
.sg-md pre code { background: none; border: 0; padding: 0; font-size: 12px; line-height: 1.5; color: var(--sg-text-secondary); white-space: pre; }
.sg-md blockquote { border-left: 3px solid var(--sg-border); padding: 2px 0 2px var(--sg-s-5); color: var(--sg-text-secondary); }
.sg-md .sg-md-scroll { overflow-x: auto; max-width: 100%; }
.sg-md table { border-collapse: collapse; font-size: 13px; }
.sg-md th, .sg-md td { border: 1px solid var(--sg-border); padding: 5px 10px; text-align: left; vertical-align: top; }
.sg-md th { background: var(--sg-surface-raised); color: var(--sg-text); }
.sg-md .sg-md-asset, .sg-md .sg-md-mermaid { display: block; margin: 0 0 0.9em; overflow-x: auto; max-width: 100%; }
.sg-md .sg-md-asset svg, .sg-md .sg-md-mermaid svg { max-width: 100%; height: auto; }
.sg-md .sg-md-save { display: block; margin: -0.5em 0 0.9em auto; font: inherit; font-size: 12px; color: var(--sg-text-secondary); background: none; border: 1px solid var(--sg-border); border-radius: 4px; padding: 1px 8px; cursor: pointer; }
.sg-md .sg-md-save:hover, .sg-md .sg-md-save:focus-visible { color: var(--sg-text); border-color: var(--sg-info-border); }
.sg-md .sg-cite-aside { margin-left: 4px; }
.sg-md .sg-cite { font-family: var(--sg-font-mono); font-size: 11px; color: var(--sg-text-secondary); background: var(--sg-code-bg); border: 1px solid var(--sg-border); border-radius: 4px; padding: 0 4px; white-space: nowrap; cursor: help; }
.sg-md a.sg-cite { color: var(--sg-text-secondary); text-decoration: none; cursor: pointer; }
.sg-md a.sg-cite:hover, .sg-md a.sg-cite:focus-visible { color: var(--sg-text); border-color: var(--sg-info-border); }
.sg-cites-off .sg-md .sg-cite-aside { display: none; }
`;

/**
 * Renders a Markdown document: marked (GFM) then DOMPurify, so no unsanitized HTML reaches the DOM.
 * Diagram SVGs of the thread (`<thread>.assets/<name>.svg`) are fetched through `loadAsset` and inlined;
 * ```mermaid blocks are drawn with a lazily imported mermaid, falling back to the code on failure.
 * Without `thread` (an agent's answer rather than the thread document) images are shown as their alt text.
 * `onRendered` runs on the sanitized DOM each time it is (re)built, before diagrams and assets are filled in.
 * With `onSaveDiagram` every Mermaid block gets a "save .mmd" button under it that hands the diagram source to the caller.
 */
export function MarkdownDoc({
  markdown,
  thread = '',
  loadAsset = noAssets,
  onRendered,
  onSaveDiagram,
  topHeading = 0,
}: {
  markdown: string;
  thread?: string;
  /** Demote headings so the shallowest becomes this level (2-6); 0 keeps them as written. */
  topHeading?: number;
  loadAsset?: (name: string) => Promise<string>;
  onRendered?: (root: HTMLElement) => void;
  /** Called with a Mermaid block's source and its 1-based position when its save button is pressed. */
  onSaveDiagram?: (code: string, position: number) => void;
}) {
  const doc = useMemo(() => renderReport(markdown, thread, topHeading), [markdown, thread, topHeading]);
  const ref = useRef<HTMLDivElement>(null);
  const [notices, setNotices] = useState<Array<{ key: string; tone: 'warn' | 'crit'; text: string }>>([]);
  // Latest loader without making a new function identity restart the rendering.
  const loadRef = useRef(loadAsset);
  loadRef.current = loadAsset;
  const renderedRef = useRef(onRendered);
  renderedRef.current = onRendered;
  const saveRef = useRef(onSaveDiagram);
  saveRef.current = onSaveDiagram;

  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    let cancelled = false;
    setNotices([]);
    root.innerHTML = doc.html;
    // Wide tables scroll inside their own box.
    root.querySelectorAll('table').forEach((t) => {
      const wrap = document.createElement('div');
      wrap.className = 'sg-md-scroll';
      t.replaceWith(wrap);
      wrap.appendChild(t);
    });
    renderedRef.current?.(root);
    const note = (key: string, tone: 'warn' | 'crit', text: string) => {
      if (!cancelled) setNotices((prev) => [...prev, { key, tone, text }]);
    };

    for (const el of Array.from(root.querySelectorAll<HTMLElement>('[data-asset]'))) {
      const name = el.dataset.asset ?? '';
      el.textContent = tr.debate.report.assetLoading(name);
      loadRef
        .current(name)
        .then((svg) => {
          if (cancelled) return;
          el.textContent = '';
          el.innerHTML = sanitizeSvg(svg);
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          el.remove();
          note(`asset:${name}`, 'crit', tr.debate.report.assetFailed(name, errorText(e)));
        });
    }

    const blocks = Array.from(root.querySelectorAll<HTMLElement>('[data-mermaid]'));
    if (blocks.length > 0) {
      if (saveRef.current) {
        blocks.forEach((el, i) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'sg-md-save';
          button.textContent = tr.debate.report.saveDiagram;
          button.addEventListener('click', () => saveRef.current?.(doc.diagrams[Number(el.dataset.mermaid)] ?? '', i + 1));
          el.after(button);
        });
      }
      void (async () => {
        const fallback = (el: HTMLElement, code: string, why: string, key: string) => {
          if (cancelled) return;
          el.textContent = '';
          const pre = document.createElement('pre');
          const codeEl = document.createElement('code');
          codeEl.textContent = code;
          pre.appendChild(codeEl);
          el.appendChild(pre);
          note(key, 'warn', tr.debate.report.diagramFailed(why));
        };
        let mermaid: MermaidApi;
        try {
          mermaid = (await import('mermaid')).default;
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: mermaidTheme(),
            // SVG text instead of HTML labels, so the sanitized SVG keeps its labels.
            htmlLabels: false,
            flowchart: { htmlLabels: false },
          });
        } catch (e) {
          blocks.forEach((el, i) => fallback(el, doc.diagrams[Number(el.dataset.mermaid)] ?? '', tr.debate.report.diagramLoadFailed(errorText(e)), `mermaid-load:${i}`));
          return;
        }
        for (const el of blocks) {
          const idx = Number(el.dataset.mermaid);
          const code = doc.diagrams[idx] ?? '';
          try {
            const { svg } = await mermaid.render(`sg-mermaid-${++mermaidSeq}`, code);
            if (cancelled) return;
            el.innerHTML = sanitizeSvg(svg);
          } catch (e) {
            // mermaid leaves its error placeholder in <body> on a syntax error.
            document.getElementById(`dsg-mermaid-${mermaidSeq}`)?.remove();
            fallback(el, code, errorText(e), `mermaid:${idx}`);
          }
        }
      })();
    }
    return () => {
      cancelled = true;
    };
  }, [doc]);

  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-3)', minWidth: 0 }}>
      <style>{SCOPED_CSS}</style>
      {notices.map((n) => (
        <Notice key={n.key} tone={n.tone}>
          {n.text}
        </Notice>
      ))}
      <div ref={ref} className="sg-md" data-testid="md-doc" />
    </div>
  );
}

import { describe, expect, it, vi } from 'vitest';
import { assetNameOf, buildStandaloneMarkdown, renderReport, sanitizeSvg, svgDataUri } from '../src/lib/report';

const T = 'thread_ab12';

describe('renderReport heading shift', () => {
  it('demotes the shallowest heading to topHeading and keeps the relative outline', () => {
    const { html } = renderReport('# A\n\n## B\n\n### C\n', T, 4);
    expect(html).toContain('<h4>A</h4>');
    expect(html).toContain('<h5>B</h5>');
    expect(html).toContain('<h6>C</h6>');
  });

  it('clamps at h6, never promotes, and leaves the document alone without a topHeading', () => {
    expect(renderReport('#### A\n\n###### B', T, 2).html).toContain('<h4>A</h4>');
    expect(renderReport('## A\n\n##### B', T, 4).html).toContain('<h6>B</h6>');
    expect(renderReport('###### A', T, 2).html).toContain('<h6>A</h6>');
    expect(renderReport('# A\n\n## B', T).html).toContain('<h1>A</h1>');
  });

  it('does not touch code that looks like a heading', () => {
    expect(renderReport('# A\n\n```sh\n# comment\n```', T, 4).html).toContain('# comment');
  });
});

describe('renderReport', () => {
  it('renders headings, GFM tables and fenced code', () => {
    const { html } = renderReport('# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```go\nfmt.Println("x")\n```\n', T);
    expect(html).toContain('<h1>Title</h1>');
    expect(html).toContain('<table>');
    expect(html).toContain('<td>1</td>');
    expect(html).toContain('<pre><code>fmt.Println("x")</code></pre>');
  });

  it('never lets raw HTML, scripts or event handlers through', () => {
    const { html } = renderReport('<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">\n\n[x](javascript:alert(3))\n\n[ok](https://example.com/a)', T);
    const dom = new DOMParser().parseFromString(html, 'text/html');
    expect(dom.querySelector('script, img')).toBeNull();
    expect(dom.querySelector('[onerror]')).toBeNull();
    expect(html).toContain('&lt;script&gt;');
    // A non-http link stays as inert text, never an anchor.
    expect(Array.from(dom.querySelectorAll('a')).map((a) => a.getAttribute('href'))).toEqual(['https://example.com/a']);
    expect(dom.body.textContent).toContain('[x](javascript:alert(3))');
    expect(html).toContain('href="https://example.com/a"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('target="_blank"');
  });

  it('keeps bracket-paren text that is not an http link as written', () => {
    const { html } = renderReport('key[0](name) and key[1](역할)', T);
    expect(html).toContain('key[0](name) and key[1](역할)');
    expect(html).not.toContain('<a');
  });

  it('turns a heading anchor link into plain text (the app uses hash routing)', () => {
    expect(renderReport('[top](#top)', T).html).not.toContain('href');
  });

  it('collects thread asset images as placeholders and drops every other image', () => {
    const r = renderReport(`![Summary](${T}.assets/summary.svg)\n\n![again](${T}.assets/summary.svg)\n\n![evil](https://evil.test/x.png)\n\n![other](thread_zz.assets/a.svg)`, T);
    expect(r.assets).toEqual(['summary.svg']);
    expect(r.html).toContain('data-asset="summary.svg"');
    expect(r.html).not.toContain('evil.test');
    expect(r.html).not.toContain('thread_zz');
  });

  it('extracts mermaid blocks and leaves a placeholder', () => {
    const r = renderReport('```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n\n```mermaid\ngraph TD\n```', T);
    expect(r.diagrams).toHaveLength(2);
    expect(r.diagrams[0]).toContain('A->>B: hi');
    expect(r.html).toContain('data-mermaid="0"');
    expect(r.html).toContain('data-mermaid="1"');
    expect(r.html).not.toContain('sequenceDiagram');
  });

  it('assetNameOf only accepts this thread and plain svg names', () => {
    expect(assetNameOf(`${T}.assets/a.svg`, T)).toBe('a.svg');
    expect(assetNameOf(`${T}.assets/../a.svg`, T)).toBeNull();
    expect(assetNameOf(`${T}.assets/a.png`, T)).toBeNull();
    expect(assetNameOf('thread_x.assets/a.svg', T)).toBeNull();
  });
});

describe('sanitizeSvg', () => {
  it('keeps shapes and strips scripts, handlers and foreign links', () => {
    const out = sanitizeSvg(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script><rect width="5" height="5" onclick="alert(2)"/><a href="javascript:alert(3)"><text>t</text></a><circle r="2" onload="alert(4)"/></svg>'
    );
    expect(out).toContain('<rect');
    expect(out).toContain('<circle');
    expect(out).not.toMatch(/<script|onclick|onload|javascript:/i);
  });
});

describe('buildStandaloneMarkdown', () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>';

  it('replaces asset links with data URIs and keeps mermaid blocks as code', async () => {
    const load = vi.fn(async () => `${svg}<script>alert(1)</script>`);
    const md = `# R\n\n![Sum](${T}.assets/summary.svg)\n\n![Sum2](${T}.assets/summary.svg)\n\n\`\`\`mermaid\ngraph TD\n\`\`\`\n`;
    const out = await buildStandaloneMarkdown(md, T, load);
    expect(load).toHaveBeenCalledTimes(1);
    expect(out).not.toContain('.assets/');
    expect(out).toContain('```mermaid\ngraph TD\n```');
    const uri = out.match(/!\[Sum\]\((data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+)\)/)?.[1];
    expect(uri).toBeTruthy();
    const decoded = atob((uri as string).split(',')[1]);
    expect(decoded).toContain('<rect');
    expect(decoded).not.toContain('<script');
    expect(out).toContain(svgDataUri(`${svg}<script>alert(1)</script>`));
  });

  it('fails the export when an asset cannot be fetched', async () => {
    await expect(buildStandaloneMarkdown(`![x](${T}.assets/a.svg)`, T, async () => Promise.reject(new Error('boom')))).rejects.toThrow('boom');
  });

  it('leaves links of other threads alone', async () => {
    const load = vi.fn(async () => svg);
    const md = '![x](thread_other.assets/a.svg)';
    expect(await buildStandaloneMarkdown(md, T, load)).toBe(md);
    expect(load).not.toHaveBeenCalled();
  });
});

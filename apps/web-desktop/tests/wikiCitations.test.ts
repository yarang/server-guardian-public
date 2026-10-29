import { describe, expect, it } from 'vitest';
import { renderReport } from '../src/lib/report';
import { decorateNarrative, parseCitation, type PageLinks } from '../src/lib/wikiCitations';

function rendered(markdown: string, title = 'Title', pages?: PageLinks) {
  const root = document.createElement('div');
  root.innerHTML = renderReport(markdown, '').html;
  const result = decorateNarrative(root, title, 'p', undefined, pages);
  return { root, ...result };
}

const chips = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('.sg-cite')).map((c) => [c.textContent, c.title]);

describe('parseCitation', () => {
  it('accepts the strong forms only', () => {
    expect(parseCitation('cli/llm-agent/debate_engine.py:712-713')).toEqual({ path: 'cli/llm-agent/debate_engine.py', location: ':712-713' });
    expect(parseCitation('apps/agent/main.go:12')).toEqual({ path: 'apps/agent/main.go', location: ':12' });
    expect(parseCitation('src/api.ts#fetchWikiPage')).toEqual({ path: 'src/api.ts', location: '#fetchWikiPage' });
    expect(parseCitation('cli/llm-agent/debate_engine.py')).toBeNull();
    expect(parseCitation('line 510')).toBeNull();
    expect(parseCitation('10:30')).toBeNull();
  });
});

describe('decorateNarrative', () => {
  it('turns a trailing parenthesised run into one hideable group without the parentheses', () => {
    const { root, citations } = rendered('Runs debates (cli/llm-agent/debate_engine.py:712-713, `cli/llm-agent/guardian.py#main`).');
    expect(citations).toBe(2);
    expect(chips(root)).toEqual([
      ['debate_engine.py:712-713', 'cli/llm-agent/debate_engine.py:712-713'],
      ['guardian.py#main', 'cli/llm-agent/guardian.py#main'],
    ]);
    const group = root.querySelector('.sg-cite-group');
    expect(group?.className).toBe('sg-cite-group sg-cite-aside');
    expect(group?.textContent).toBe('debate_engine.py:712-713, guardian.py#main');
    expect(root.querySelector('p')?.textContent).toBe('Runs debatesdebate_engine.py:712-713, guardian.py#main.');
  });

  it('keeps a citation that is part of the sentence visible and leaves code blocks alone', () => {
    const { root } = rendered('See `apps/agent/main.go:12` for the loop.\n\n```\ncall apps/agent/main.go:12\n```');
    const group = root.querySelector('.sg-cite-group');
    expect(group?.className).toBe('sg-cite-group');
    expect(root.querySelector('p')?.textContent).toBe('See main.go:12 for the loop.');
    expect(root.querySelector('pre')?.textContent).toBe('call apps/agent/main.go:12');
  });

  it('does not match inside a longer path or a URL-like token and ignores bare paths', () => {
    const { root, citations } = rendered('Bare cli/llm-agent/guardian.py and time 10:30 stay text.');
    expect(citations).toBe(0);
    expect(root.querySelector('.sg-cite')).toBeNull();
  });

  it('drops a leading title that repeats the page title and ids the h2/h3 headings', () => {
    const { root, headings } = rendered('# Title\n\n## Role\n\ntext\n\n### Detail\n\n## Flow', 'Title');
    expect(root.querySelector('h1')).toBeNull();
    expect(headings).toEqual([
      { id: 'p-h0', text: 'Role', level: 2 },
      { id: 'p-h1', text: 'Detail', level: 3 },
      { id: 'p-h2', text: 'Flow', level: 2 },
    ]);
    expect(root.querySelector('#p-h1')?.tagName).toBe('H3');
  });

  it('keeps a leading title that differs from the page title', () => {
    const { root } = rendered('# Something else\n\ntext', 'Title');
    expect(root.querySelector('h1')?.textContent).toBe('Something else');
  });
});

describe('decorateNarrative page links', () => {
  const pages: PageLinks = { known: new Set(['cli-llm-agent', 'feature-x']), href: (id) => `#/debates/wiki/${id}` };
  const links = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('.sg-wikilink')).map((a) => [a.tagName, a.dataset.page, a.getAttribute('href')]);

  it('links [[id]] in text and in inline code, and marks an unknown id as missing', () => {
    const { root } = rendered('## Related pages\n\n- [[cli-llm-agent]] the overview\n- `[[feature-x]]` the design\n- [[nope]] gone', 'Title', pages);
    expect(links(root)).toEqual([
      ['A', 'cli-llm-agent', '#/debates/wiki/cli-llm-agent'],
      ['A', 'feature-x', '#/debates/wiki/feature-x'],
      ['SPAN', 'nope', null],
    ]);
    expect(root.querySelector('li')?.textContent).toBe('cli-llm-agent the overview');
  });

  it('links every id while the page list is not loaded and leaves code blocks and plain text alone', () => {
    const { root } = rendered('See [[a-b]].\n\n```\n[[c-d]]\n```\n\nIn [x] and [[Not An Id]].', 'Title', { known: null, href: pages.href });
    expect(links(root)).toEqual([['A', 'a-b', '#/debates/wiki/a-b']]);
    expect(root.querySelector('pre')?.textContent).toBe('[[c-d]]');
  });

  it('leaves [[id]] as text without page links', () => {
    const { root } = rendered('See [[a-b]].');
    expect(root.querySelector('.sg-wikilink')).toBeNull();
    expect(root.textContent).toContain('[[a-b]]');
  });
});

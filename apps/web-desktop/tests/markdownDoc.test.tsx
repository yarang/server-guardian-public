import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const m = vi.hoisted(() => ({
  loaded: 0,
  initialize: vi.fn(),
  render: vi.fn(),
}));
vi.mock('mermaid', () => {
  m.loaded += 1;
  return { default: { initialize: m.initialize, render: m.render } };
});
import { MarkdownDoc } from '../src/components/MarkdownDoc';
import { ko } from '../src/i18n/ko';

const T = 'thread_ab12';
const okSvg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="5" height="5"/></svg>';

beforeEach(() => {
  m.initialize.mockReset();
  m.render.mockReset();
});

describe('MarkdownDoc', () => {
  it('renders headings and tables, without importing mermaid when there is no diagram', async () => {
    const before = m.loaded;
    render(<MarkdownDoc markdown={'# Hello\n\n| a | b |\n|---|---|\n| 1 | 2 |'} thread={T} loadAsset={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Hello' })).toBeTruthy();
    expect(screen.getByRole('table').parentElement?.className).toContain('sg-md-scroll');
    await new Promise((r) => setTimeout(r, 20));
    expect(m.loaded).toBe(before);
  });

  it('strips script and onerror from markdown', () => {
    const { container } = render(<MarkdownDoc markdown={'<script>window.__x=1</script>\n\n<img src=x onerror="window.__x=2">\n\ntext'} thread={T} loadAsset={vi.fn()} />);
    expect(container.querySelector('script, img')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
    expect((window as unknown as { __x?: number }).__x).toBeUndefined();
  });

  it('inlines a fetched asset SVG sanitized, and shows a notice when one fails', async () => {
    const load = vi.fn(async (name: string) => {
      if (name === 'bad.svg') throw new Error('gone');
      return `${okSvg.replace('</svg>', '')}<script>alert(1)</script><rect onclick="alert(2)" width="1" height="1"/></svg>`;
    });
    const { container } = render(<MarkdownDoc markdown={`![A](${T}.assets/good.svg)\n\n![B](${T}.assets/bad.svg)`} thread={T} loadAsset={load} />);
    await waitFor(() => expect(container.querySelector('[data-asset="good.svg"] svg rect')).toBeTruthy());
    expect(container.querySelector('script')).toBeNull();
    expect(container.innerHTML).not.toMatch(/onclick/i);
    expect(await screen.findByText(ko.debate.report.assetFailed('bad.svg', 'gone'))).toBeTruthy();
  });

  it('renders mermaid lazily with strict security and sanitizes the result', async () => {
    m.render.mockResolvedValue({ svg: `${okSvg.replace('</svg>', '')}<script>alert(1)</script></svg>` });
    const { container } = render(<MarkdownDoc markdown={'```mermaid\nsequenceDiagram\n  A->>B: hi\n```'} thread={T} loadAsset={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('[data-mermaid] svg rect')).toBeTruthy());
    expect(m.initialize).toHaveBeenCalledWith(expect.objectContaining({ startOnLoad: false, securityLevel: 'strict', theme: 'dark' }));
    expect(m.render).toHaveBeenCalledWith(expect.stringMatching(/^sg-mermaid-/), expect.stringContaining('A->>B: hi'));
    expect(container.querySelector('script')).toBeNull();
  });

  it('falls back to the code with a warning when mermaid rejects the diagram', async () => {
    m.render.mockRejectedValue(new Error('Parse error'));
    const { container } = render(<MarkdownDoc markdown={'```mermaid\nnot a diagram\n```'} thread={T} loadAsset={vi.fn()} />);
    expect(await screen.findByText(ko.debate.report.diagramFailed('Parse error'))).toBeTruthy();
    expect(container.querySelector('[data-mermaid] pre code')?.textContent).toBe('not a diagram');
  });

  it('adds a save button under each diagram only when asked, and hands over the source', async () => {
    m.render.mockResolvedValue({ svg: okSvg });
    const md = '```mermaid\ngraph TD\n  A-->B\n```\n\n```mermaid\nnot a diagram\n```';
    const plain = render(<MarkdownDoc markdown={md} thread={T} loadAsset={vi.fn()} />);
    await waitFor(() => expect(plain.container.querySelector('[data-mermaid] svg')).toBeTruthy());
    expect(plain.container.querySelector('.sg-md-save')).toBeNull();
    plain.unmount();

    const save = vi.fn();
    const { container } = render(<MarkdownDoc markdown={md} thread={T} loadAsset={vi.fn()} onSaveDiagram={save} />);
    await waitFor(() => expect(container.querySelector('[data-mermaid] svg')).toBeTruthy());
    const buttons = screen.getAllByRole('button', { name: ko.debate.report.saveDiagram });
    expect(buttons).toHaveLength(2);
    expect(buttons[0].previousElementSibling?.hasAttribute('data-mermaid')).toBe(true);
    fireEvent.click(buttons[1]);
    expect(save).toHaveBeenCalledWith('not a diagram', 2);
    fireEvent.click(buttons[0]);
    expect(save).toHaveBeenLastCalledWith('graph TD\n  A-->B', 1);
  });
});

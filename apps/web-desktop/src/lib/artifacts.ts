import type { ArtifactFile } from '../types';

/** How the dashboard shows a file: Markdown and Mermaid through MarkdownDoc, SVG only as an image. */
export type ArtifactView = 'markdown' | 'mermaid' | 'svg';

export function artifactView(file: ArtifactFile): ArtifactView | null {
  return file.kind === 'markdown' || file.kind === 'mermaid' || file.kind === 'svg' ? file.kind : null;
}

/** The Office formats the hub derives from a Markdown file (`--artifact-format`). */
export const OFFICE_FORMATS = ['docx', 'pptx'] as const;
export type OfficeFormat = (typeof OFFICE_FORMATS)[number];

/** The name of the Office file the hub derives from a Markdown file (`overview.md` -> `overview.pptx`). */
export function derivedNameFor(markdownPath: string, format: OfficeFormat): string {
  return `${markdownPath.replace(/\.md$/i, '')}.${format}`;
}

/** A live Markdown file whose `format` file does not exist yet in the same version; only these get that button. */
export function canMakeDerived(file: ArtifactFile, files: ArtifactFile[], format: OfficeFormat): boolean {
  return file.kind === 'markdown' && !file.expired && !files.some((f) => f.path === derivedNameFor(file.path, format));
}

/** The format of a derived Office file, or null: such a file is only ever saved, never shown. */
export function derivedFormat(file: ArtifactFile): OfficeFormat | null {
  return OFFICE_FORMATS.find((format) => format === file.kind) ?? null;
}

const EXTENSION_VIEWS: Record<string, ArtifactView> = { '.md': 'markdown', '.mmd': 'mermaid', '.svg': 'svg' };

/** The view for a file name that is not stored yet (a `write_artifact` proposal), by the hub's extension rule. */
export function artifactViewForName(name: unknown): ArtifactView | null {
  if (typeof name !== 'string') return null;
  const dot = name.lastIndexOf('.');
  return (dot >= 0 && EXTENSION_VIEWS[name.slice(dot).toLowerCase()]) || null;
}

/** A `.mmd` file is shown as a fenced block, which MarkdownDoc draws with mermaid (sanitized, strict mode). */
export function mermaidAsMarkdown(source: string): string {
  // A fence longer than any backtick run inside the source cannot be closed early by the content.
  const longest = Math.max(2, ...(source.match(/`+/g) ?? []).map((run) => run.length));
  const fence = '`'.repeat(longest + 1);
  return `${fence}mermaid\n${source.replace(/\n+$/, '')}\n${fence}\n`;
}

/**
 * SVG text as an `<img>` source. An image never runs scripts or loads other resources, and a data URL needs no
 * `blob:` entry in the page's CSP. The text is untrusted and is never put into the DOM as markup.
 */
export function svgDataUrl(svg: string): string {
  const bytes = new TextEncoder().encode(svg);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:image/svg+xml;base64,${btoa(binary)}`;
}

/** Route params of the Artifacts tab: `#/debates/artifacts[/<thread>[/v<n>[/<file>]]]`. */
export interface ArtifactRoute {
  thread?: string;
  version?: number;
  file?: string;
}

export function parseArtifactRoute(params: string[]): ArtifactRoute {
  const [thread, v, file] = params;
  const m = v ? /^v([1-9][0-9]{0,8})$/.exec(v) : null;
  if (!thread) return {};
  if (!m) return { thread };
  return { thread, version: Number(m[1]), file: file || undefined };
}

export function artifactParams(r: ArtifactRoute): string[] {
  if (!r.thread) return ['artifacts'];
  if (r.version === undefined) return ['artifacts', r.thread];
  return r.file ? ['artifacts', r.thread, `v${r.version}`, r.file] : ['artifacts', r.thread, `v${r.version}`];
}

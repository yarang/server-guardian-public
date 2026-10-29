import { invoke } from '@tauri-apps/api/core';

/**
 * The export folder of the desktop shell (docs/design/desktop-export.md). The webview never names a path: Rust opens
 * the folder dialog and stores the root, and only a display path comes back here.
 */

/** True inside the Tauri shell; the browser build has no export folder. */
export function inDesktopShell(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

/** The stored export folder, or null when none is set or it no longer exists. */
export function exportGetRoot(): Promise<string | null> {
  return invoke<string | null>('export_get_root');
}

/** Opens the native folder dialog; resolves to the chosen folder, or null when cancelled. */
export function exportChooseRoot(): Promise<string | null> {
  return invoke<string | null>('export_choose_root');
}

/** Forgets the folder; the folder and its files stay. */
export function exportClearRoot(): Promise<void> {
  return invoke<void>('export_clear_root');
}

/** Opens the export folder in the OS file manager. */
export function exportReveal(): Promise<void> {
  return invoke<void>('export_reveal');
}

/** One file of an `export_save` call; `relPath` is relative to `<root>/<project>/<thread>/` (docs/design/desktop-export.md 3.3). */
export interface ExportFile {
  relPath: string;
  contents: string;
}

/** `updated` only comes from automatic export: a file that was still exactly what Guardian wrote was replaced in place. */
export type ExportOutcome = 'written' | 'skipped' | 'renamed' | 'updated';

export interface ExportFileResult {
  relPath: string;
  outcome: ExportOutcome;
  /** The relative path the file ended up at: the suffixed name when renamed, the existing file when skipped. */
  savedAs: string;
}

/**
 * Writes files below the stored root. Rust builds the path from `project`, `thread` and each `relPath` and refuses the
 * whole call when any of them is malformed, so nothing is written for a bad list.
 */
export function exportSave(project: string, thread: string, files: ExportFile[]): Promise<ExportFileResult[]> {
  return invoke<ExportFileResult[]>('export_save', { project, thread, files });
}

export interface ExportCounts {
  written: number;
  skipped: number;
  renamed: number;
  updated: number;
}

export const noCounts = (): ExportCounts => ({ written: 0, skipped: 0, renamed: 0, updated: 0 });

export function countResults(results: ExportFileResult[], into: ExportCounts = noCounts()): ExportCounts {
  for (const r of results) into[r.outcome] += 1;
  return into;
}

/** Automatic export (docs/design/desktop-export.md 3.7): the switch lives in Rust next to the root. */
export interface AutoState {
  enabled: boolean;
  /** Unix ms when the switch was turned on; only results updated after it are exported. */
  sinceMs: number;
}

export interface AutoRecent {
  atMs: number;
  project: string;
  thread: string;
  written: number;
  skipped: number;
  renamed: number;
  updated: number;
}

export function exportAutoGet(): Promise<AutoState> {
  return invoke<AutoState>('export_auto_get');
}

export function exportAutoSet(enabled: boolean): Promise<AutoState> {
  return invoke<AutoState>('export_auto_set', { enabled });
}

/** `thread -> marker` (the result's `updated_at`) of what was exported automatically in a project. */
export function exportAutoMarks(project: string): Promise<Record<string, string>> {
  return invoke<Record<string, string>>('export_auto_marks', { project });
}

export function exportAutoRecent(): Promise<AutoRecent[]> {
  return invoke<AutoRecent[]>('export_auto_recent');
}

/** Like {@link exportSave} but a report Guardian wrote and nobody edited is updated in place; records `marker`. */
export function exportAutoSave(project: string, thread: string, marker: string, files: ExportFile[]): Promise<ExportFileResult[]> {
  return invoke<ExportFileResult[]>('export_auto_save', { project, thread, marker, files });
}

/** Sent after every automatic write so the Settings list can reload. */
export const AUTO_EXPORT_EVENT = 'guardian:auto-export';

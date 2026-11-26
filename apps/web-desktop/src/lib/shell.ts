/** True inside the Tauri desktop shell (its webview defines `__TAURI_INTERNALS__`); false in a browser. */
export function inDesktopShell(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

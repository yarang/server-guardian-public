import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const invoke = vi.hoisted(() => vi.fn());
vi.mock('@tauri-apps/api/core', () => ({ invoke }));
import { GuardianRoot } from '@server-guardian/ui';
import { ko } from '../src/i18n/ko';
import { inDesktopShell } from '../src/lib/exportFolder';
import { ExportFolderSection } from '../src/pages/ExportFolderSection';

const t = ko.shell.exportFolder;
const mount = () =>
  render(
    <GuardianRoot>
      <ExportFolderSection />
    </GuardianRoot>
  );

const defaultAuto = { enabled: false, sinceMs: 0, recent: [] as unknown[] };

/** Answers the export commands from a tiny in-memory root, the way the Rust side would. */
function fakeRust(initial: string | null, picks: Array<string | null> = [], autoInit: Partial<typeof defaultAuto> = {}) {
  let root = initial;
  const auto = { ...defaultAuto, ...autoInit };
  invoke.mockImplementation(async (cmd: string, args?: unknown) => {
    if (cmd === 'export_get_root') return root;
    if (cmd === 'export_choose_root') {
      const next = picks.shift() ?? null;
      if (next) root = next;
      return next;
    }
    if (cmd === 'export_clear_root') {
      root = null;
      auto.enabled = false;
      return undefined;
    }
    if (cmd === 'export_reveal') return undefined;
    if (cmd === 'export_auto_get') return { enabled: auto.enabled && root !== null, sinceMs: auto.sinceMs };
    if (cmd === 'export_auto_recent') return auto.recent;
    if (cmd === 'export_auto_set') {
      auto.enabled = (args as { enabled: boolean }).enabled;
      if (auto.enabled) auto.sinceMs = 1_000;
      return { enabled: auto.enabled, sinceMs: auto.sinceMs };
    }
    throw new Error(`unexpected command ${cmd}`);
  });
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  invoke.mockReset();
});

describe('inDesktopShell', () => {
  it('is true only when the Tauri bridge exists', () => {
    expect(inDesktopShell()).toBe(false);
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    expect(inDesktopShell()).toBe(true);
  });
});

describe('ExportFolderSection', () => {
  it('offers to choose a folder when none is stored', async () => {
    fakeRust(null, ['/Users/me/Guardian']);
    mount();
    expect(await screen.findByText(t.none)).toBeTruthy();
    expect(screen.queryByRole('button', { name: t.forget })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: t.choose }));
    expect(await screen.findByText('/Users/me/Guardian')).toBeTruthy();
    expect(screen.getByRole('button', { name: t.change })).toBeTruthy();
  });

  it('shows the stored folder and keeps it when the dialog is cancelled', async () => {
    fakeRust('/Users/me/Guardian', [null]);
    mount();
    expect(await screen.findByText('/Users/me/Guardian')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: t.change }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('export_choose_root'));
    expect(screen.getByText('/Users/me/Guardian')).toBeTruthy();
  });

  it('forgets the folder and can reveal it', async () => {
    fakeRust('/Users/me/Guardian');
    mount();
    await screen.findByText('/Users/me/Guardian');
    fireEvent.click(screen.getByRole('button', { name: t.show }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('export_reveal'));
    fireEvent.click(screen.getByRole('button', { name: t.forget }));
    expect(await screen.findByText(t.none)).toBeTruthy();
    expect(invoke).toHaveBeenCalledWith('export_clear_root');
  });

  it('passes no path to Rust: the webview only asks, Rust picks and stores', async () => {
    fakeRust(null, ['/x']);
    mount();
    await screen.findByText(t.none);
    fireEvent.click(screen.getByRole('button', { name: t.choose }));
    await screen.findByText('/x');
    for (const call of invoke.mock.calls.filter((c) => /root|reveal/.test(String(c[0])))) expect(call).toHaveLength(1);
  });

  it('shows the error when Rust refuses', async () => {
    fakeRust(null);
    invoke.mockImplementation(async (cmd: string) => {
      if (cmd === 'export_get_root') return null;
      throw 'choose a folder, not the top of a drive';
    });
    mount();
    await screen.findByText(t.none);
    fireEvent.click(screen.getByRole('button', { name: t.choose }));
    expect(await screen.findByText(new RegExp('choose a folder, not the top of a drive'))).toBeTruthy();
  });
});

describe('automatic export switch', () => {
  it('is off by default, turns on through Rust and lists nothing yet', async () => {
    fakeRust('/Users/me/Guardian');
    mount();
    const box = (await screen.findByLabelText(t.auto.label)) as HTMLInputElement;
    await waitFor(() => expect(box.disabled).toBe(false));
    expect(box.checked).toBe(false);
    expect(screen.getByText(t.auto.recentNone)).toBeTruthy();
    fireEvent.click(box);
    await waitFor(() => expect(box.checked).toBe(true));
    expect(invoke).toHaveBeenCalledWith('export_auto_set', { enabled: true });
  });

  it('is not offered without a folder, and forgetting the folder switches it off', async () => {
    fakeRust('/Users/me/Guardian', [], { enabled: true });
    mount();
    const box = (await screen.findByLabelText(t.auto.label)) as HTMLInputElement;
    await waitFor(() => expect(box.checked).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: t.forget }));
    await screen.findByText(t.none);
    expect(screen.queryByLabelText(t.auto.label)).toBeNull();
  });

  it('lists the recent automatic writes with their counts', async () => {
    fakeRust('/Users/me/Guardian', [], {
      enabled: true,
      recent: [{ atMs: 1_700_000_000_000, project: 'server-guardian', thread: 'thread_a1', written: 1, skipped: 0, renamed: 0, updated: 2 }],
    });
    mount();
    expect(await screen.findByText('server-guardian/thread_a1')).toBeTruthy();
    expect(screen.getByText(new RegExp(t.result(1, 0, 0, 2).replace(/[()]/g, '\\$&')))).toBeTruthy();
  });
});

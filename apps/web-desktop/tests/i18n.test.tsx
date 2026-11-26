import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { en } from '../src/i18n/en';
import { ko } from '../src/i18n/ko';
import { DEFAULT_LANGUAGE, LANGUAGE_STORAGE_KEY, LANGUAGES, isLanguage } from '../src/i18n';

type Tree = Record<string, unknown>;

/** Every key path of a catalog, with "fn" for the functions (their arity is part of the shape). */
function shape(node: unknown, path = ''): string[] {
  if (typeof node === 'function') return [`${path}:fn${node.length}`];
  if (node === null || typeof node !== 'object') return [path];
  return Object.keys(node as Tree)
    .sort()
    .flatMap((k) => shape((node as Tree)[k], path ? `${path}.${k}` : k));
}

/** Calls every function of a catalog with sample arguments and collects what does not yield text. */
function blanks(node: unknown, path = ''): string[] {
  if (typeof node === 'function') {
    const args = Array.from({ length: node.length }, () => 2);
    try {
      const out = (node as (...a: unknown[]) => unknown)(...args);
      return typeof out === 'string' && out.trim() ? [] : [path];
    } catch {
      return []; // functions that need structured arguments are covered by the screens' own tests
    }
  }
  if (typeof node === 'string') return node.trim() ? [] : [path];
  if (node === null || typeof node !== 'object') return [];
  return Object.entries(node as Tree).flatMap(([k, v]) => blanks(v, path ? `${path}.${k}` : k));
}

afterEach(() => {
  window.localStorage.clear();
  vi.resetModules();
});

describe('Language catalogs', () => {
  it('English has exactly the keys and function arities of Korean', () => {
    expect(shape(en)).toEqual(shape(ko));
  });

  it('every English string and sample function result is text', () => {
    expect(blanks(en)).toEqual([]);
  });

  it('names every language in its own language, the same in every catalog', () => {
    for (const code of Object.keys(LANGUAGES)) {
      expect(ko.shell.language.names[code]).toBeTruthy();
      expect(en.shell.language.names[code]).toBe(ko.shell.language.names[code]);
    }
  });
});

describe('Language choice', () => {
  it('defaults to Korean and ignores an unknown saved value', async () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'xx');
    const i18n = await import('../src/i18n');
    expect([i18n.language, i18n.locale, i18n.tr === i18n.LANGUAGES.ko.catalog]).toEqual([DEFAULT_LANGUAGE, 'ko-KR', true]);
    expect(isLanguage('xx')).toBe(false);
  });

  it('the saved language picks the catalog, the locale and <html lang>', async () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en');
    const i18n = await import('../src/i18n');
    expect([i18n.language, i18n.locale]).toEqual(['en', 'en-US']);
    expect(i18n.tr.common.save).toBe(en.common.save);
    expect(document.documentElement.lang).toBe('en');
    expect(document.title).toBe(en.shell.documentTitle);
    const format = await import('../src/lib/format');
    expect(format.LOCALE).toBe('en-US');
  });

  it('setLanguage saves and reloads, and does neither when storage fails or nothing changes', async () => {
    const i18n = await import('../src/i18n');
    const reload = vi.fn();
    expect(i18n.setLanguage('ko', reload)).toBe(true);
    expect(reload).not.toHaveBeenCalled();
    expect(i18n.setLanguage('en', reload)).toBe(true);
    expect([window.localStorage.getItem(LANGUAGE_STORAGE_KEY), reload.mock.calls.length]).toEqual(['en', 1]);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    window.localStorage.removeItem(LANGUAGE_STORAGE_KEY);
    expect(i18n.setLanguage('en', reload)).toBe(false);
    expect(reload.mock.calls.length).toBe(1);
    vi.restoreAllMocks();
  });

  it('a screen renders in English when English is saved', async () => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, 'en');
    const { GuardianRoot } = await import('@server-guardian/ui');
    const { SettingsPage } = await import('../src/pages/SettingsPage');
    // The hub connection group holds the URL panel; the language panel lives under Display.
    const endpoint = render(
      <GuardianRoot>
        <SettingsPage agentUrl="http://hub.test:9870" onEditEndpoint={() => undefined} section="endpoint" />
      </GuardianRoot>,
    );
    expect(screen.getAllByText(en.shell.settingsPage.endpointTitle).length).toBeGreaterThan(0);
    expect(screen.queryByText(ko.shell.settingsPage.endpointTitle)).toBeNull();
    endpoint.unmount();
    render(
      <GuardianRoot>
        <SettingsPage agentUrl="http://hub.test:9870" onEditEndpoint={() => undefined} section="display" />
      </GuardianRoot>,
    );
    expect((screen.getByRole('combobox', { name: en.shell.language.label }) as HTMLSelectElement).value).toBe('en');
    expect(screen.queryByLabelText(ko.shell.language.label)).toBeNull();
  });
});

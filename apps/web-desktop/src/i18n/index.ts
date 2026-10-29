/**
 * The dashboard's language: which message catalog the components read (`tr`), and the locale of dates and numbers.
 *
 * The language is chosen once, when the app loads: the operator's saved choice (localStorage), else Korean. Switching
 * it saves the choice and reloads the page (setLanguage), so every screen, table column and constant built at module
 * load uses the same catalog. Tests run in the default language and assert on `ko.…`, which is `tr` there.
 *
 * Adding a language: write src/i18n/<code>.ts with the Messages shape (the compiler lists every missing key), add it
 * to LANGUAGES below with its BCP 47 locale, and add its name to every catalog's shell.language.names.
 */
import { en } from './en';
import { ko } from './ko';
import type { Messages } from './types';

export const LANGUAGES = {
  ko: { catalog: ko as Messages, locale: 'ko-KR' },
  en: { catalog: en, locale: 'en-US' },
} as const;

export type Language = keyof typeof LANGUAGES;
export const DEFAULT_LANGUAGE: Language = 'ko';
/** localStorage key of the chosen language (per browser). */
export const LANGUAGE_STORAGE_KEY = 'guardian.language';

export function isLanguage(value: unknown): value is Language {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(LANGUAGES, value);
}

/** The saved language, or the default when nothing usable is saved or storage is unavailable. */
export function storedLanguage(): Language {
  try {
    const v = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
    return isLanguage(v) ? v : DEFAULT_LANGUAGE;
  } catch {
    return DEFAULT_LANGUAGE;
  }
}

/** The language of this page load. */
export const language: Language = storedLanguage();
/** The active message catalog: what components display. */
export const tr: Messages = LANGUAGES[language].catalog;
/** Locale of every date and number the dashboard formats (lib/format.ts). */
export const locale: string = LANGUAGES[language].locale;

if (typeof document !== 'undefined') {
  document.documentElement.lang = language;
  document.title = tr.shell.documentTitle; // index.html carries the default language's title until this runs
}

/**
 * Saves the language and reloads the page with it. Returns false when it could not be saved (private mode): the
 * page then stays in its current language rather than reloading into the same one.
 */
export function setLanguage(next: Language, reload: () => void = () => window.location.reload()): boolean {
  if (next === language) return true;
  try {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, next);
  } catch {
    return false;
  }
  reload();
  return true;
}

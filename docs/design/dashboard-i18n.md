# Web dashboard languages (message catalogs)

English | [한국어](dashboard-i18n.ko.md)

The user-visible copy of `apps/web-desktop` lives in per-language message catalogs (`src/i18n/`). Korean (the default) and English are supported today. No i18n library is used (no added dependency). The language is chosen once when the page loads; changing it reloads the page.

## Layout

```
apps/web-desktop/src/i18n/
  index.ts         language choice: LANGUAGES, tr, language, locale, setLanguage, <html lang> and document.title
  types.ts         the Messages type: the shape every catalog must have
  uiMessages.ts    sets the design-system copy to the active language (setUiMessages(tr.shell.ui))
  ko.ts            Korean catalog entry point. Rules, style and glossary comment + sections composed into `ko`
  ko/common.ts     words shared by many screens (save, cancel, loading…, relative time "5 min ago", ...)
  ko/shell.ts      app frame, navigation, hub connection / sign-in / settings, language choice, design-system copy (ui), API error text
  ko/ops.ts        overview, approvals, command run
  ko/fleet.ts      nodes, node detail / registration, groups, SSH audit
  ko/debate.ts     debates page, start form, results, actions, personas / teams / projects
  ko/agents.ts     agents monitor (run list, lanes, timeline)
  ko/wiki.ts       code wiki tab
  en.ts            English catalog entry point (`en: Messages`)
  en/*.ts          the same section files as ko/
```

Components get the active catalog with `import { tr } from '../i18n'` and read `tr.fleet.…`. They do not import a catalog file (`ko`, `en`) directly. A sentence with parts is a function (`` (n: number) => `${n}개` ``), not string concatenation.

## Language choice (`src/i18n/index.ts`)

- `LANGUAGES` maps each language code to its catalog and BCP 47 locale: `ko` → `ko-KR`, `en` → `en-US`. `DEFAULT_LANGUAGE` is `ko`.
- On page load the localStorage key `guardian.language` (`LANGUAGE_STORAGE_KEY`) is read. When it is missing, names an unknown language, or storage is unavailable, the language is Korean. The choice is stored per browser, not on the hub.
- The result is exported as `language` (the code), `tr` (the active catalog) and `locale`. `LOCALE` in `lib/format.ts` is this `locale`, so `fmtDateTime()` and every other date and number use the active language's format.
- On load, `<html lang>` is set to the language and `document.title` to `tr.shell.documentTitle`. `index.html` carries the default language's title and `lang="ko"` until this code runs.
- `setLanguage(next)` saves the choice and reloads the page. It does nothing when the language is unchanged. When saving fails (private window and similar) it returns `false` and does not reload, so the page does not reload into the same language.
- Why reload: screens, table columns and constants built at module load (route labels and others) then all use the same catalog. There is no path that swaps the catalog at run time.
- The copy of the design-system components (`packages/guardian-ui`: copy button, table headers, aria labels, ...) is set to the active language by `i18n/uiMessages.ts` at startup with `setUiMessages(tr.shell.ui)`. `main.tsx` and `tests/setup.ts` import that file.

## Language selector in Settings

Settings > Hub connection (`#/settings/endpoint`) has a "Language" panel (`LanguageSection` in `SettingsPage.tsx`).

- The select box lists the languages of `LANGUAGES`, each by its own name (`shell.language.names`, e.g. 한국어, English).
- Picking one calls `setLanguage` and the page reopens in that language. The hint (`shell.language.hint`) says the choice is stored in this browser only.
- When it cannot be saved, the page stays in its current language and shows a warning (`shell.language.saveFailed`).

## The Messages type (`src/i18n/types.ts`)

`Messages` is the type of the Korean catalog (`Catalog = typeof ko`) with its string literal types widened to `string`. Every other catalog is declared with that type (`export const en: Messages = …`). Therefore:

- A missing, extra or mistyped key is a compile error.
- A function key must have the same arguments and return type. Each language builds its sentences with its own functions (English plurals such as `n === 1 ? 'item' : 'items'`, Korean a number plus a counter noun).

## Raw-value rule

1. **Data stays raw.** Status and kind values that the API returns or code compares (`APPROVED`, `DISPUTED`, `PROPOSED`, `fresh` / `stale` / `broken`, `running` / `failed`, event type names, ...) are never translated in state, props, URLs, comparisons or sent payloads. They are the same in every language.
2. **Only display labels follow the active language.** Map the raw value through a catalog table and show an unknown value as itself (`tr.x.label[value] ?? value`), never as a blank.
3. **Keep visible what operators see in the CLI, logs and API.** Show the raw value next to the label as a `title` tooltip or small mono text.
4. **Never translated**: identifiers, API field names, event type names, routes, command / log / terminal output, code, file paths, host names, model and provider names, and terms operators type (sha, sudo, SSH key names, CLI flags, JSON, HTTP codes). Server error text returned by the hub, the API or Python is shown as is. The product name "Server Guardian" stays.
5. **Dates and times** use `fmtDateTime()` (the active language's locale) and `ago()` / `duration()` / `shortUptime()` from `lib/format.ts`. Their unit words come from the catalog. UTC and relative-time arithmetic do not depend on the language.

## Style

- Korean: concise 해요체 mixed with noun forms. Buttons and labels are noun or verb stems (저장, 삭제, 다시 쓰기, 새로 고침); sentences and confirmations use 해요체 ("이 페이지를 삭제할까요?", "저장했어요"). No trailing period on labels and titles.
- English: plain sentence case, no trailing period on labels and titles.
- The glossary is the header comment of `ko.ts` (허브, 노드, 플릿, 그룹, 토론, 승인, 위키 ...). The rules and the glossary live only in `ko.ts`; `en.ts` points to them.

## Tests

- `tests/i18n.test.tsx`:
  - The English catalog has exactly the keys and function arities of the Korean one.
  - Every English string, and every function result for sample arguments, is non-empty text.
  - Every language has the same name in every catalog (its own name).
  - Without a saved value, or with an unknown one, the language is Korean; a saved language picks the catalog, the locale, `<html lang>`, `document.title` and `format.LOCALE`.
  - `setLanguage` saves and reloads, and does neither when the language is unchanged or saving fails.
  - With English saved, the Settings page renders in English.
- The other tests run in the default language (Korean) and assert on imported `ko.…` values instead of literals (`tr` is `ko` there). Wording edits do not break them.

## Adding a string

1. Add the key to the matching section file `ko/<area>.ts`, grouped by screen (`common.ts` when three or more screens use it).
2. Add the same key in English to `en/<area>.ts`. If it is missing, the compiler reports it and `i18n.test.tsx` fails.
3. Reference it from the component as `{tr.fleet.nodes.addNode}`. No hard-coded strings.
4. In tests, import `ko` and assert on `ko.…` values instead of literals.
5. The English defaults of the design system (`packages/guardian-ui`) stay for the library's own tests and stories. The app passes the active language's copy through `shell.ui` (→ `setUiMessages`) or props.

## Adding a language

1. Write `src/i18n/<code>.ts` (and section files `<code>/*.ts`) with the `Messages` type. The compiler lists every missing key.
2. Add the language to `LANGUAGES` in `index.ts` with its BCP 47 locale.
3. Add the language's name, in that language, to `shell.language.names` of every catalog. It then appears in the Settings selector.
4. The key and blank checks in `tests/i18n.test.tsx` compare only `en` today; add the new catalog to them.

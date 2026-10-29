# Office export: DOCX and PPTX from reports, diagrams for every project

English | [한국어](office-export.ko.md)

Status: decisions approved 2026-10-27; D2 (DOCX) built 2026-10-27, deployed 2026-10-28 as `d84562c`, opened in Word by the operator 2026-10-28; D3 (PPTX) built 2026-10-28, deployed 2026-10-28 as `b50af88`, opened in PowerPoint and confirmed by the operator 2026-10-28; D1 rendering already deployed in the dashboard; "Save .mmd" on report diagrams built 2026-10-28, deployed 2026-10-28 as `b50af88` (section 5), the operator confirmed the button and the .mmd file 2026-10-28; option (c), the report snapshot ("Keep as document"), approved and built 2026-10-28, deployed 2026-10-28 as `f6497bd` (sections 5 and 6). The operator asked whether results can also come as `.pptx` /
`.docx` and whether SVG and Mermaid files can be an output. Two decisions were made on the proposal: **(1)** start with
direct OOXML written with the standard library (option A), and move to `python-docx` / `python-pptx` (option B) only if
that proves too thin; **(2)** conversion is requested **by a button**, not run by the engine after approval.

## 1. Today

- A debate result is a Markdown report. Design projects get versioned artifacts (`.md`, `.mmd`, `.svg`) in
  `design-artifacts/` from approved `write_artifact` actions, shown in the Artifacts tab and exported by E2/E3. A code
  or ops project gets such a version only when a person presses "Keep as document" on its report (section 5, option
  (c)); until then its report lives only under `decisions/projects/`.
- No code writes DOCX or PPTX. The Python engine is standard-library only (`CLAUDE.md`, standing decisions).

## 2. Decisions

1. **Markdown stays canonical.** DOCX and PPTX are derived files. A derived file is never edited and never read back.
2. **Direct OOXML first (A).** `zipfile` plus fixed XML templates, no dependency, so the hub release layout does not
   change. Option B (`python-docx`, `python-pptx`, which pull in `lxml`) is decided only if A cannot do what is asked;
   it also needs the open question of `design-project.md` (how a dependency reaches the ARM hub release) answered.
3. **A button starts a conversion.** The engine does not convert after a result is approved. The Results tab and the
   Artifacts tab get a "Make DOCX" / "Make PPTX" action on a stored Markdown version.

## 3. Rules the button implies

It is a new write endpoint behind `AGENT_TOKEN`, so it is narrow:

- **Input is a stored, validated Markdown version** (`thread`, `version`, `name`), never client-supplied text. The
  validator and the secret scan of `artifact_ops.py` have already passed on it; the converter re-reads the file and
  checks its sha256 against the manifest before using it.
- **Output is a new file in the same version directory**, named `<name>.docx` / `<name>.pptx`, listed in the manifest
  with the **source file name and sha256**. Versions stay immutable: the conversion adds a file to its own version
  record, it does not create a new version, and refuses to overwrite.
- **Macro-free formats only.** No `.docm` / `.pptm`, no VBA parts, no external relationships: no hyperlink to a
  remote target that the Markdown validator would refuse, no external image. Links that are in-document or `http(s)`
  text stay text with the URL shown.
- **Download is always `Content-Disposition: attachment`** with a fixed MIME type; the dashboard never renders it
  inline. The existing `artifactFile` rules (sha256 check on every read, symlinks refused) apply unchanged.
- **Audit.** Each conversion writes an event naming who pressed the button (the actor the API already records), the
  source sha256 and the result sha256.
- **Size bound.** The converter refuses a source above the artifact byte limit and a result above the safety limit.

## 4. Phases

| Phase | Content | Done when |
|---|---|---|
| D2 | `office_docx.py`: Markdown subset (headings, paragraphs, lists, tables, code blocks, emphasis) to DOCX; `--artifact-convert`; `POST /api/threads/{thread}/artifacts/{version}/convert`; the button | The file opens in Word and LibreOffice; the sha256 and audit rules above hold; a corpus of malformed Markdown never yields an invalid package |
| D3 | `office_pptx.py`: one slide per first- or second-level heading with its body; title slide; tables on their own slides | Same checks for PPTX; the file opens in PowerPoint (confirmed by the operator 2026-10-28) |
| D1 | `.mmd` / `.svg` attachments for code and ops results; Mermaid rendering in the dashboard | Rendering deployed; "Save .mmd" deployed (option (b)); no new store, see section 5 |
| D4 | Option B, only if A is too thin | Not planned |

D2 comes first because it is exactly what option A covers and needs no dashboard dependency.

## 5. Open points

- **Mermaid rendering in the dashboard (D1).** The proposal said "render on the client with `securityLevel: strict`",
  which adds the `mermaid` npm package to `apps/web-desktop`. The operator approved that dependency on 2026-10-27
  (pin an exact version, `securityLevel: strict`, watch the bundle size). **Built and deployed** (commit `18b8669`, before `d84562c`): `mermaid` 11.17.2 is pinned,
  `MarkdownDoc.tsx` imports it lazily on the first ```mermaid block with `securityLevel: 'strict'` and shows the code
  with a warning when a diagram is rejected (`tests/markdownDoc.test.tsx`). It is a separate chunk (`mermaid.core-*.js`, 667 kB, 160 kB gzip) and
  diagram types load their own chunks on demand (cytoscape 444 kB, katex 261 kB, one 691 kB), so a page without a
  diagram loads none of it.
- **Attachments for code and ops results (D1, option (b) approved 2026-10-28, deployed as `b50af88`).** Only `kind: "design"` projects have versioned stores, so a
  debate report of a code or ops project has nowhere to keep a `.mmd` or `.svg`. Options: (a) skip it, since Mermaid
  blocks inside the report already render; (b) show the diagram blocks of a report and offer each as a `.mmd`
  download, with no new store; (c) a versioned store for code and ops reports, a larger change with its own retention
  and audit rules. The operator approved (b) on 2026-10-28:
  `MarkdownDoc` takes an optional `onSaveDiagram`, and the debate report (`ReportView`, the report dialog and the
  Results tab) passes it, so each Mermaid block gets a "Save .mmd" button under it that writes
  `<thread>-diagram-<n>.mmd` with the block's source (browser download, or `save_download` in the desktop app, which
  writes text and so works for `.mmd`). Nothing is stored, no API changes, other users of `MarkdownDoc` (agent answers,
  wiki) show no button. Not built: `.svg` attachments of rendered diagrams.
- **Report snapshot for code and ops results (D1, option (c), approved 2026-10-28 with the scope "snapshot store",
  built, deployed as `f6497bd`).** The report Markdown becomes a `report.md` version in the existing artifact store, so the
  Word and PowerPoint buttons work on it. Chosen over extracting diagrams into the version, `.svg` attachments and
  on-the-fly conversion without storage. Rules: only a person starts it (the "Keep as document" button of a report;
  no agent write path, no new action kind); the hub re-reads the report from disk itself, so no text travels through
  the request; the same validators, secret scan, sha256 manifest and size limit as every artifact apply; each keep is
  an immutable version; an audit line is written; code and ops projects expire old versions after the default 30 days,
  the latest version never. Design projects are refused (their documents are their artifacts).
- Images in DOCX (the Markdown validator allows relative image targets): in D2 an image is written as its alt text
  until the image bytes can be read from the same version directory with the same checks.
- Whether a PPTX theme is needed beyond one fixed neutral template.

## 6. Implementation notes

### D1 (c): report snapshot (built 2026-10-28, deployed as `f6497bd`)

- **Pieces.** `report_snapshot.py` (`keep_report`: reads `decisions/projects/<project>/<thread>.md` without following
  symlinks, requires a regular UTF-8 file under the safety limit, then `ArtifactStore.write("report.md", ...)` with
  `base_version` set to the latest version, agent `report-snapshot`), `guardian.py --report-keep` (JSON; exit 2
  refused by the validators or a design project, 3 unknown project or no report, 4 same text already kept),
  `POST /api/threads/{thread}/report/keep` in `report_keep.go` (maps 2/3/4 to 400/404/409 through the shared config
  response path, audit line `report keep <thread>` with the version and sha256), and the "Keep as document" button in
  `ReportView` (report dialog and Results tab; hidden when the project list says the report's project is a design project). After a keep the button row shows a
  link to `#/debates/artifacts/<thread>`, where the existing Word and PowerPoint buttons work.
- **Same text twice is a 409**, and a changed report makes a new version. A converted `.docx` or `.pptx` carried in the
  latest version does not make a re-keep look new: only the `report.md` hash is compared.
- **Behaviour changes in shared code.** `--artifact-convert` no longer refuses non-design projects (the thread must
  have a kept report, otherwise 404), and `--artifacts-prune` applies the default retention to non-design projects
  instead of skipping them. The Artifacts viewer route opens for every project kind by link; the tab itself is still
  shown only for design projects, and a code or ops project reaches the viewer from the keep link or the Results tab.
- **Known limits.** Images of the report (`<thread>.assets/...`) are not copied: the validator only warns, the keep
  succeeds, and the dashboard shows the warning count and the warning texts. A report containing raw HTML (for example a bare `<TZ>`
  placeholder) is refused with the validator's message; nothing is rewritten to make it pass. The assets and the
  `.mmd` of diagrams are not part of the version.
- **Checked:** `test_report_snapshot.py` (keep, same text, changed text, validators, symlink and non-regular file,
  design refusal, ops project, convert after keep, CLI exit codes), Go route tests with the fake CLI (status mapping,
  audit line, unknown thread), web tests (keep, warnings, 409, refusal, disabled without a report, hidden for design,
  viewer open for a code project, button hidden for a design project in the report dialog), and a Go test that a kept version of a non-design project is listed in Results, readable and convertible (the Go routes have no project-kind gate). Not checked in a real browser or on the hub.

### D2: Markdown to DOCX (built 2026-10-27, deployed 2026-10-28)

- **Pieces.** `office_ooxml.py` (XML text hygiene, deterministic zip writer, `check_package`), `office_docx.py`
  (`markdown_to_docx`), `ArtifactStore.convert` in `artifact_ops.py`, `guardian.py --artifact-convert NAME --project P
  --thread T --artifact-version artifact@vN [--artifact-format docx]`, route `POST
  /api/threads/{thread}/artifacts/{version}/convert` with body `{"name": "<file>.md"}` (`artifact_handler.go`), and the
  "Make Word document" button in the Artifacts tab.
- **Derived name is `<stem>.docx`** (`overview.md` gives `overview.docx`), so the pair is visible in the listing. The
  manifest entry carries `source`, `source_sha256` and `converted_at`. Existing `.docx` is never overwritten (exit 4,
  HTTP 409). Exit codes of the CLI: 2 invalid or refused, 3 unknown project, thread, version or file (including an
  expired one), 4 already exists; the API maps them to 400, 404, 409.
- **Derived files follow their source.** A new version carries a `.docx` forward only while its source `.md` is
  unchanged in that version; a rewritten source drops the stale `.docx` (a derived file never describes text it was
  not built from). Pruning removes the `.docx` with the rest of its version. `ArtifactStore.read` returns text only,
  so a `.docx` can never reach a debate agent, and `write_artifact` still refuses the extension.
- **Gates.** Source must match its manifest sha256 and stay within the artifact limit (262144 bytes); the result goes
  through `check_package` (intact zip, safe unique names, no VBA or embedded objects, every part has a content type,
  all XML parses, only internal relationships whose targets exist) and a 2 MiB cap before the store keeps it. The
  file is hard-linked into place first (it fails when the name appeared meanwhile, so nothing is overwritten), then the
  manifest is rewritten atomically; a failed manifest write removes the file again. The same
  source gives the same bytes (fixed zip timestamps), so the same sha256.
- **Serving.** `.docx` is in the served types of `artifact_handler.go` but always `Content-Disposition: attachment`,
  fixed MIME, `nosniff` and the sandbox CSP, with or without `?download=1`. The audit line reads `artifact convert
  <name>@v<n> -> docx` with `source_sha256=... result_sha256=...` and the actor of the request.
- **Not in the standard library path:** images become their alt text, links become text with the URL shown (no
  hyperlink relationship), HTML is text. Tables, lists (up to nine levels, kept as indentation with a literal marker), block quotes,
  fenced code and task-list marks are supported.
- **Dashboard.** The button is on Markdown rows of the Artifacts tab only, not in the Results tab: debate reports of
  code and ops projects are not stored versions, so there is no version to attach a file to; Results lists design
  artifacts joined by thread id and reaches the tab through "View artifacts". After a conversion the listing is read
  again. A `.docx` row is download-only (never fetched as text or shown) and the export folder skips it, because the
  desktop export writes text. **The desktop app cannot download a `.docx` yet** (its save command writes text and a
  Blob link does not download inside its webview); it says so and points to the browser.
- **Checked:** unit tests (converter corpus of adversarial Markdown, store rules, CLI exit codes), Go route tests
  (CLI arguments, audit line, refusals, credentials, attachment-only serving), web tests (button rules, conversion and
  refresh, binary download path), `scripts/ci.sh` as a whole. An earlier sample rendered correctly in LibreOffice
  (Korean glyph gaps there came from missing fonts in the sandbox). **Opening a file in Microsoft Word was checked by the operator on 2026-10-28 (a converted `dashboard-ui` artifact, downloaded from the browser dashboard); no defect was reported.**
  The done condition of D2 is therefore met for LibreOffice and the structural checks only.

### D3: Markdown to PPTX (built 2026-10-28, deployed as `b50af88`)

- **Pieces.** `office_pptx.py` (`markdown_to_pptx`), `md_parse.py` (the Markdown block and inline parser, moved out of
  `office_docx.py` so both converters read a document the same way), `office_ooxml.check_package(data, main_part,
  max_parts)` (a slide is two parts, so PPTX passes its own limit; DOCX keeps 64), and `ArtifactStore.convert(version,
  name, target)` generalised from DOCX (`--artifact-format docx|pptx`, body field `format` on the route). The derived
  name is `<stem>.pptx`; every rule of D2 about names, manifest entries, carrying forward, exit codes, gates and
  attachment-only serving applies unchanged. The DOCX bytes are identical before and after the refactor (checked by
  hash on a corpus).
- **Slide rules.** A title slide only when the first block is a first-level heading (its first short paragraph, up to
  160 width units, becomes the subtitle). Each first- or second-level heading starts a slide; deeper headings stay
  inside the slide as bold lines. Text before the first heading goes on an untitled slide. Blank headings and rules
  make no slide; a document with no text is refused.
- **Nothing is cut.** A slide holds 11 body lines (a wide CJK glyph counts as two width units, a code line as 0.75
  of a line); more content continues on a slide titled `Title (2)`, `Title (3)`, splitting paragraphs at whitespace. A
  table gets its own slide, 8 rows per slide with the header repeated; more than 20 columns is refused, and so is a
  deck of more than 60 slides (the message names "slides"). Text is split before its inline Markdown is read, so the
  widths include markup characters (an extra slide at worst) and a long paragraph that is cut inside an emphasis or
  link span shows the markers as literal text on both slides. This is a known roughness, not data loss.
- **Look.** One fixed neutral master with two layouts (title, title and body), Calibri with Malgun Gothic for East Asian text, Courier New for code. Bullets
  use the master bullet; literal markers (numbers, task marks) switch the bullet off so they do not double.
- **Dashboard.** `MakeOffice` shows one button per format still missing for a Markdown row ("Make Word document",
  "Make PowerPoint"); the derived-file branch is generic over `.docx` and `.pptx`. The desktop app cannot download
  either yet.
- **Checked:** `test_office_pptx.py` (slide rules, package structure, store rules, CLI), Go route tests for
  `format: "pptx"` and for attachment-only serving of both formats, web tests, and a render of a sample deck to eight pages in
  LibreOffice. **Opening a file in Microsoft PowerPoint was confirmed by the operator on 2026-10-28, as D2 was in Word.**

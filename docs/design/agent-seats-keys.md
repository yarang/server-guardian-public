# Agent seats and API key assignment: set by the user, keys held by the hub (design)

English | [한국어](agent-seats-keys.ko.md)

Status: proposed 2026-10-29; scope and key storage were decided by the operator the same day (see "Decisions").
Nothing built (phases K1-K4).
Origin: "The roles of the agents and the assignment of API keys should be something the user can set."

## Problem

- Which provider (and so which key) sits in a debate seat is written by hand into the team file
  (`"providers": ["zai"]`, `docs/design/debate-teams.md`). The dashboard team editor shows the list but a person
  cannot see whether the provider is ready, where its credential comes from, or which keys a team depends on.
- Keys live in `llm.env` files, one per host, filled by `scripts/deploy-llm-env.sh`, which replaces the whole file.
  Adding or rotating a key means editing a local file by hand and re-deploying; nothing in the dashboard shows
  that a key exists, which seats use it, or when it was set.
- One provider has one key per host. Two teams cannot use two different keys of the same provider.

## Scope

In scope: the **debate seats** of a team, that is `expert`, `proposers`, `critic`, `judge`, and the `stages` and
`sides` of the pipeline and versus flows. Per seat a person sets the provider (exists today), the **key** (new) and,
as an engine change, the model (see "Open points").

Not in scope:

- The **code-editing agent** (`scripts/zai-agent.sh`): it stays a working rule in `CLAUDE.md`, with its key on the
  operator's machine.
- Persona text and persona names (the persona editor), the shape of a flow (`docs/design/flow-templates.md`).
- CLI **logins** (see the next section).

## Two kinds of credential

| Kind | Examples | Set in the dashboard | Where it lives |
| --- | --- | --- | --- |
| Key (environment variable) | `ZAI_API_KEY`, `OPENAI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` | yes | the hub key store; workers receive it per job from K3 |
| CLI login | `~/.claude/.credentials.json`, `~/.codex/auth.json` | no, never | the host that did the login; never copied, never read by the hub (`CLAUDE.md`, rules that must not be broken) |

The screen shows one source per provider on a host: **hub key**, **host login** or **missing**. `zai` has no login
source: without a key it is `missing`, and a run fails instead of borrowing the claude login (`zai-provider.md`).

## Key store

A file outside the data directory: `~/.guardian/llm-keys.json`, mode 600, override `GUARDIAN_LLM_KEYS_FILE` (tests
isolate it the way they isolate `GUARDIAN_LLM_ENV`). It sits next to `llm.env` on purpose: the nightly backup packs
`~/.guardian/data` and two fleet files only (`docs/operations/backup-restore.md`), so a store inside `data/` would
put every key into every tarball. Written atomically (temp file, mode 600, rename), under the same lock style as the
inventory.

An entry:

```json
{
  "id": "zai-main",
  "provider": "zai",
  "env": "ZAI_API_KEY",
  "fingerprint": "9c1e0a7b",
  "base_url": "https://api.z.ai/api/anthropic",
  "default": true,
  "set": "2026-10-29T09:12:00Z",
  "rotated": "2026-10-29T09:12:00Z",
  "value": "..."
}
```

- `id`: `[a-z0-9-]{1,40}`, the name teams refer to. `provider` must be a known provider; `env` must be a name that
  `env_policy` lets that provider see (`PROVIDER_PREFIXES`), never `AGENT_*`, `GUARDIAN_*`, `TYPESAFE_*`.
- `fingerprint`: the first 8 hex characters of `sha256("sg-key-v1\0" + value)`. It tells two keys apart and shows that
  a rotation took. It is never a part of the value (no "last four").
- `base_url`: optional, only for providers that have one (`zai`, `ollama`). Whoever can repoint an endpoint can read
  the key it is sent with, so the URL is as protected as the key: https (or loopback) only, **changing it requires
  entering the key again**, and every change is audited with the old and new host name.
- `default`: at most one entry per provider. The default is used by every seat that pins no key.
- `value` is write-only. No endpoint, event, report, log line or error message returns it; the API returns metadata
  and `used_by` (the teams that pin the entry).

## Seat resolution

A seat names a provider list in order, as today. A seat may also pin a key: `"key": "<id>"` (K2).

For each provider the runner picks the credential in this order:

1. The pinned entry, when the seat pins one for **this** provider. A pinned entry that is missing, deleted or of
   another provider **refuses the start** (validation at save and again at start). It never falls back to the default
   key or to a login.
2. The `default` entry of that provider in the store.
3. The hub's `llm.env` value of the same name (legacy, until K4).
4. The host's CLI login (`claude`, `codex`, `agy`). Never for `zai`.

The pin applies to its own provider only; other providers in the chain resolve with 2-4. Failure fallback between
providers is unchanged. The store wins over `llm.env` where both define a name, because the store is what the screen
shows. `deploy-llm-env.sh` keeps overwriting `llm.env` only; it no longer touches what the dashboard manages.

**Migration:** `guardian.py --keys-import` reads the hub's `llm.env` once and creates one entry per known provider
variable (`imported-<name>`, marked default). It deletes nothing; the lines in `llm.env` are removed at K4 with the
operator's OK.

## Delivery

- **Hub.** `LLMRunner` builds each call's environment from `os.environ`, `llm.env` and the store, then applies
  `env_policy.provider_env` exactly as today, so a provider still sees only its own prefixes. A pinned key needs a
  per-call override (`run_session_prompt(..., key_env=...)`): an **engine change**, not configuration.
- **Workers** (K3, protocol 3). The hub sends the entry's environment variable inside the stdin payload of `run`, which
  the worker passes to the CLI child on its stdin (`worker.py`, `communicate(payload)`) and keeps in memory only. The
  worker must not write it to the job directory, the call log (`calls.jsonl`), `argv`, or any reply. A test plants a
  sentinel key and greps the whole worker directory after a job. Workers still on protocol 2 keep using the host
  `llm.env` until K4; the probe reports the protocol (`probe-worker.sh`).
- The transport from the dashboard to the CLI is already safe for a secret: `configSave` hands the request body to
  the Python CLI on **stdin** (`runConfigCLI(body, ...)`), never in `argv`, so `ps` does not show it. The key routes
  add an audit line (`source=keys`, id, provider, action, actor; no value) and are excluded from any request logging.

## API

Go relays and Python validates, as for teams.

- `GET /api/keys` -> `{keys:[{id, provider, env, fingerprint, base_url, default, set, rotated, used_by:[team ids]}],
  providers:[{id, installed, source: "hub key" | "host login" | "missing", ready, key}]}` (`--keys --json`)
- `POST /api/keys` (`--key-save`): create or replace. `value` is required on create, on rotation and when `base_url`
  changes; omitted, only `default` may change. Replies with metadata only.
- `DELETE /api/keys/{id}` (`--key-delete`): `409` with the team ids while a team pins it.
- Team files: `members[].key` is optional and referenced by id only; `validate_lineup` checks that it exists and that
  the member's provider list contains the entry's provider. Unknown key -> error at save.
- Events: `agent_started.key` carries the id (never the value or the fingerprint of a different entry).

## Dashboard

- **Settings > Keys** (`#/settings/keys`): a list of entries with provider, environment name, fingerprint, `default`
  chip, set/rotated times, and the teams that use it; a provider table with the source chip per provider; "Set / replace
  key" opens a form with a password field (`autocomplete="new-password"`), never prefilled, cleared after submit.
- **Team editor**: each seat row shows provider, model (when K2b exists) and a key picker limited to entries of that
  provider; an empty picker reads "default key of the provider". A read-only **seat table** per team (seat -> provider ->
  model -> key) is shown above the editor and in the project's team picker.
- A value is never stored in `localStorage`, drafts, `debatePrefill`, `RerunInputs`, URL, events or reports.
  Strings go into both catalogs (`ko`, `en`) in a new `keys` group.

## Glossary (proposals until the operator confirms)

`자리` (seat), `키` (an entry of the key store), `로그인` (host login of a CLI), `기본 키` (the default entry). The
existing terms (team, persona, provider, judge) stay.

## Phases

- **K1** Key store, write-only API, Settings > Keys. Hub only; `--keys-import`. Resolution steps 2-4 (no seat pin yet).
- **K2** Seat `key` in the team file, validation and the editor; `key_env` in the runner. **K2b** (optional, own
  decision) seat `model`.
- **K3** Per-job delivery to workers (protocol 3), re-probe of the six workers, sentinel test.
- **K4** Remove keys from host `llm.env` files and from the hub's. Needs the operator's OK.

Every phase goes through `scripts/ci.sh` and the deploy chain; K1-K2 touch Python, Go and the dashboard, K3 the
worker and the installer.

## Decisions

1. **Scope: debate seats only** (operator, 2026-10-29). The code-editing agent stays a rule in `CLAUDE.md`.
2. **Keys are held by the hub and are write-only** (operator, 2026-10-29). *This revises decision 3 of
   [zai-provider.md](zai-provider.md) ("the key is deployed to the Tier-1 worker hosts as well as the hub") and the
   contract in `worker.py` ("uses this host's own provider logins"): from K3 the hub passes a key per job, from K4 the
   hosts keep none.* Until then both ways work.
3. **CLI logins stay on their host** and are never set, shown or copied by the dashboard.
4. A pinned key that cannot be used refuses the start; it never falls back to another credential.

## Open points

- A model per seat. Today `llm_runner` has no per-call model; the zai tiers are global (`ZAI_{OPUS,SONNET,HAIKU}_MODEL`)
  and the other CLIs choose their own. A seat `model` is an engine change and needs its own decision (K2b).
- A "Test key" button (one PONG call through the key) and what it costs against the shared Codex quota.
- Who may set keys: the API has one bearer token (`AGENT_TOKEN`), so any holder can rotate a key. A separate role
  would be a security change of its own.
- No encryption at rest beyond mode 600 and the sandbox, the same as `llm.env` today.
- Several keys per provider in one seat (rotation on 429): not planned; the retry time of z.ai 429 is unmeasured.

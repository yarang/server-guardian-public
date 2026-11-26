#!/usr/bin/env bash
# ==============================================================================
# Server Guardian - CI checks (the same script runs locally and in GitHub Actions)
#
# Usage:
#   scripts/ci.sh                 # go python web shell config (tauri only when named)
#   scripts/ci.sh go python       # selected sections
#
# Sections:
#   go      gofmt (changed-by-us files must stay formatted), go vet, go test -race, linux/arm64 build
#           of apps/agent, and gofmt / go vet / go test -race / linux builds of tools/goindex
#   python  the cli/llm-agent unittest suite and scripts/test_*.py
#   web     design system (typecheck, tests, build) and dashboard (build, tests)
#   tauri   desktop app: release build of apps/web-desktop/src-tauri (no installer bundles) and its Rust tests
#   shell   bash -n and shellcheck for scripts/*.sh, scripts/lib/*.sh and deploy/*/*.sh
#   config  JSON syntax of configs/, personas/teams and flow templates validated by guardian.py, and the bilingual documents
#           (every name.md has name.ko.md, language lines, relative links resolve)
#
# In CI (CI=true) a missing tool is an error; locally it is a skipped check with a warning.
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

FAILED=()
SKIPPED=()
IN_CI="${CI:-false}"

section() { printf '\n==> %s\n' "$1"; }
fail() { FAILED+=("$1"); printf '  FAILED: %s\n' "$1" >&2; }
# Every step block runs as "set +e; ( set -e; ... ); checked $? <name>". The obvious
# "( ... ) || fail" is wrong: bash ignores set -e inside any command that is part of a ||
# list, so a failing step (tsc, go vet) was hidden whenever the block's last step passed.
checked() {
    local rc="$1"
    set -e
    [ "${rc}" -eq 0 ] && return 0
    fail "$2"
    return 1
}
need() {
    command -v "$1" >/dev/null 2>&1 && return 0
    if [ "${IN_CI}" = "true" ]; then
        fail "$1 is not installed"
    else
        SKIPPED+=("$1")
        printf '  skipped: %s is not installed\n' "$1" >&2
    fi
    return 1
}

run_go() {
    section "go (apps/agent, tools/goindex)"
    need go || return 0
    set +e
    (
        set -e
        cd apps/agent
        # Files written before formatting was enforced; the rest of the module must stay gofmt-clean.
        legacy="metrics.go model.go"
        unformatted="$(gofmt -l . | grep -vxF -e metrics.go -e model.go || true)"
        if [ -n "${unformatted}" ]; then
            echo "  not gofmt-formatted: ${unformatted}" >&2
            exit 1
        fi
        echo "  gofmt ok (legacy exceptions: ${legacy})"
        go vet ./...
        go test -race ./...
        # Same version stamp as scripts/install-agent-service.sh, so the release build path is checked too.
        version="$(git describe --always --dirty 2>/dev/null || echo unknown)"
        for arch in arm64 amd64; do
            GOOS=linux GOARCH="${arch}" CGO_ENABLED=0 go build -ldflags "-X main.AgentVersion=${version}" -o /dev/null .
        done
    )
    checked $? "go" || true
    # The compiler-assisted Go index helper (docs/design/debate-code-index-wiki.md, P2.6): its own module.
    set +e
    (
        set -e
        cd tools/goindex
        unformatted="$(gofmt -l .)"
        if [ -n "${unformatted}" ]; then
            echo "  tools/goindex not gofmt-formatted: ${unformatted}" >&2
            exit 1
        fi
        echo "  gofmt ok (tools/goindex)"
        go vet ./...
        go test -race ./...
        # Same cross builds as scripts/install-agent-service.sh.
        for arch in arm64 amd64; do
            GOOS=linux GOARCH="${arch}" CGO_ENABLED=0 go build -o /dev/null .
        done
    )
    checked $? "go: tools/goindex" || true
}

run_python() {
    section "python (cli/llm-agent, scripts)"
    need python3 || return 0
    set +e
    (
        set -e
        cd cli/llm-agent
        data_dir="$(mktemp -d)"
        trap 'rm -rf "${data_dir}"' EXIT
        # GUARDIAN_DATA_DIR keeps tests away from ~/.guardian. GUARDIAN_LLM_PROVIDERS=none means no
        # provider CLI is ever called (tests that need one mock it), so CI makes no real LLM calls.
        GUARDIAN_DATA_DIR="${data_dir}" GUARDIAN_LLM_PROVIDERS=none python3 -m unittest discover -p 'test_*.py' -q
        # Persona form guide (docs/design/agent-characters.md, C0): warning only, never fails CI.
        python3 persona_lint.py | grep -E '^(warn|persona lint:)' || true
        # Helpers next to the shell scripts (credential bundle crypto).
        python3 -m unittest discover -s ../../scripts -p 'test_*.py' -q
    )
    checked $? "python" || true
}

run_web() {
    section "web (packages/guardian-ui, apps/web-desktop)"
    need npm || return 0
    set +e
    (
        set -e
        cd packages/guardian-ui
        npm ci --no-audit --no-fund
        npm run typecheck
        npm test
        npm run build
    )
    checked $? "web: guardian-ui" || return 0
    set +e
    (
        set -e
        cd apps/web-desktop
        npm ci --no-audit --no-fund
        npm run build
        npm test
    )
    checked $? "web: web-desktop" || true
}

run_tauri() {
    section "tauri (apps/web-desktop/src-tauri)"
    need cargo || return 0
    need npm || return 0
    set +e
    (
        set -e
        cd apps/web-desktop
        [ -d node_modules ] || npm ci --no-audit --no-fund
        # Compiles the release binary with the built dashboard embedded (beforeBuildCommand runs
        # `npm run build`); --no-bundle skips the .deb/.AppImage/.dmg installers.
        npx tauri build --no-bundle
        (cd src-tauri && cargo test --quiet)
    )
    checked $? "tauri" || true
}

run_shell() {
    section "shell (scripts, deploy)"
    local f
    for f in scripts/*.sh scripts/lib/*.sh deploy/*/*.sh; do
        bash -n "${f}" || fail "bash -n ${f}"
    done
    if need shellcheck; then
        # Errors and warnings only; style notes do not fail the build.
        shellcheck -x --severity=warning scripts/*.sh scripts/lib/*.sh deploy/*/*.sh || fail "shellcheck"
    fi
}

run_config() {
    section "config (configs/)"
    need python3 || return 0
    # User-facing documents come in English (name.md) and Korean (name.ko.md), each linking to the other right under
    # its title, and their relative links must resolve. Agent instructions (CLAUDE.md, SKILL.md, .agents, .claude,
    # .design-sync) are English only, and so are external references kept verbatim (docs/design/reference/).
    python3 - <<'PY' || fail "config: bilingual documents"
import pathlib, re, subprocess, sys
skip = re.compile(r"(^|/)(CLAUDE(\.local)?\.md|SKILL\.md)$|^\.(agents|claude|design-sync)/|/\.claude/|^docs/design/reference/")
files = [f for f in subprocess.run(["git", "ls-files", "*.md"], capture_output=True, text=True).stdout.split()
         if not skip.search(f)]
link = re.compile(r"\]\(([^)#\s]+)(#[^)]*)?\)")
bad = 0
def problem(msg):
    global bad
    bad += 1
    print(f"  {msg}", file=sys.stderr)
for name in files:
    path = pathlib.Path(name)
    korean = name.endswith(".ko.md")
    other = path.with_name(path.name[:-6] + ".md") if korean else path.with_name(path.name[:-3] + ".ko.md")
    if not other.exists():
        problem(f"{name}: missing {other}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0] == "---":  # front matter (the design-system component docs) comes before the title
        lines = lines[next((i for i, l in enumerate(lines[1:], 1) if l == "---"), 0) + 1:]
    head = "\n".join(l for l in lines if l.strip())[:600]
    wanted = f"[English]({other.name}) | 한국어" if korean else f"English | [한국어]({other.name})"
    if wanted not in head:
        problem(f"{name}: no language line '{wanted}' under the title")
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        for target, _ in link.findall(re.sub(r"`[^`]*`", "", line)):  # examples inside inline code are not links
            if re.match(r"[a-z]+:", target) or target.startswith("/"):
                continue
            if not (path.parent / target).exists():
                problem(f"{name}: broken link {target}")
print(f"  {len(files)} documents checked")
sys.exit(1 if bad else 0)
PY
    python3 - <<'PY' || fail "config: JSON syntax"
import json, pathlib, sys
bad = 0
for path in sorted(pathlib.Path("configs").rglob("*.json")):
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        bad += 1
        print(f"  {path}: {exc}", file=sys.stderr)
# The example always; the local snapshot (git-ignored, scripts/pull-inventory.sh) when this checkout has one.
for name in ("configs/servers.example.json", "configs/servers.json"):
    path = pathlib.Path(name)
    if name.endswith("servers.json") and not path.exists():
        continue
    nodes = json.loads(path.read_text(encoding="utf-8"))
    ids = [s.get("id") for s in nodes]
    if len(ids) != len(set(ids)) or not all(ids):
        bad += 1
        print(f"  {name}: node ids must be present and unique", file=sys.stderr)
    hubs = [s["id"] for s in nodes if s.get("guardian_role") == "hub"]
    if len(hubs) != 1:
        bad += 1
        print(f"  {name}: exactly one hub expected, found {hubs}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
    set +e
    (
        set -e
        data_dir="$(mktemp -d)"
        trap 'rm -rf "${data_dir}"' EXIT
        # Built-in personas and teams must load and validate (custom dirs point at an empty temp dir).
        GUARDIAN_DATA_DIR="${data_dir}" python3 cli/llm-agent/guardian.py --personas --json >/dev/null
        GUARDIAN_DATA_DIR="${data_dir}" python3 cli/llm-agent/guardian.py --teams --json | python3 -c '
import json, sys
teams = json.load(sys.stdin)["teams"]
errors = [(t["id"], e) for t in teams for e in t["errors"]]
for team_id, error in errors:
    print(f"  team {team_id}: {error}", file=sys.stderr)
sys.exit(1 if errors else 0)'
    )
    checked $? "config: personas/teams" || true
    # The package roles (configs/package-roles.json) drive the protection of package actions: they must validate.
    python3 cli/llm-agent/guardian.py --package-roles | python3 -c '
import json, sys
out = json.load(sys.stdin)
if not out.get("ok"):
    print("  package-roles.json: " + str(out.get("error")), file=sys.stderr)
sys.exit(0 if out.get("ok") else 1)'
    checked $? "config: package roles" || true
    # The install bundles (configs/package-bundles.json) must validate too: names are checked per package manager.
    # A bundle not reviewed for 180 days (GUARDIAN_BUNDLE_MAX_AGE_DAYS) is only a warning.
    python3 cli/llm-agent/guardian.py --package-bundles | python3 -c '
import json, sys
out = json.load(sys.stdin)
if not out.get("ok"):
    print("  package-bundles.json: " + str(out.get("error")), file=sys.stderr)
for item in out.get("stale", []):
    print("  WARNING: bundle %s was last reviewed %s (%d days ago, limit %d): check its package names and versions, then "
          "update \"reviewed\"" % (item["name"], item["reviewed"], item["age_days"], out["max_age_days"]), file=sys.stderr)
sys.exit(0 if out.get("ok") else 1)'
    checked $? "config: package bundles" || true
    # The builtin command sets (configs/command-sets) must validate (including a bash -n syntax check); a set not
    # reviewed for 180 days (GUARDIAN_COMMAND_SET_MAX_AGE_DAYS) is only a warning. The data dir is not touched.
    GUARDIAN_DATA_DIR="$(mktemp -d)" python3 cli/llm-agent/guardian.py --command-sets | python3 -c '
import json, sys
out = json.load(sys.stdin)
if not out.get("ok"):
    print("  command-sets: " + str(out.get("error")), file=sys.stderr)
for item in out.get("stale", []):
    print("  WARNING: command set %s was last reviewed %s (%d days ago, limit %d): re-read its script, then update "
          "\"reviewed\"" % (item["id"], item["reviewed"], item["age_days"], out["max_age_days"]), file=sys.stderr)
sys.exit(0 if out.get("ok") else 1)'
    checked $? "config: command sets" || true
    # The builtin verdict sources (configs/verdicts) must validate; one not reviewed for 180 days
    # (GUARDIAN_VERDICT_SOURCE_MAX_AGE_DAYS) is only a warning. The data dir is not touched.
    GUARDIAN_DATA_DIR="$(mktemp -d)" python3 cli/llm-agent/guardian.py --verdict-sources | python3 -c '
import json, sys
out = json.load(sys.stdin)
if not out.get("ok"):
    print("  verdict sources: " + str(out.get("error")), file=sys.stderr)
for item in out.get("stale", []):
    print("  WARNING: verdict source %s was last reviewed %s (%d days ago, limit %d): check its settings, then update "
          "\"reviewed\"" % (item["id"], item["reviewed"], item["age_days"], out["max_age_days"]), file=sys.stderr)
sys.exit(0 if out.get("ok") else 1)'
    checked $? "config: verdict sources" || true
    # The builtin flow templates (configs/flow-templates, may be empty) must validate against the builtin teams; one not
    # reviewed for 180 days (GUARDIAN_FLOW_TEMPLATE_MAX_AGE_DAYS) is only a warning. The data dir is not touched.
    GUARDIAN_DATA_DIR="$(mktemp -d)" python3 cli/llm-agent/guardian.py --flow-templates --json | python3 -c '
import json, sys
templates = json.load(sys.stdin)["templates"]
bad = False
for t in templates:
    for error in t["errors"]:
        bad = True
        print("  flow template %s: %s" % (t["id"], error), file=sys.stderr)
    for warning in t["warnings"]:
        print("  WARNING: flow template %s: %s" % (t["id"], warning), file=sys.stderr)
sys.exit(1 if bad else 0)'
    checked $? "config: flow templates" || true
    # Every builtin team and flow template must convert into a flow that validates (docs/design/flow-graph.md, G1),
    # and so must the builtin flows (configs/flows, may be missing). The data dir is not touched.
    GUARDIAN_DATA_DIR="$(mktemp -d)" python3 cli/llm-agent/guardian.py --flows | python3 -c '
import json, sys
flows = json.load(sys.stdin)["flows"]
bad = False
for f in flows:
    for error in f["errors"]:
        bad = True
        print("  flow %s (%s): %s" % (f["id"], f["source"], error), file=sys.stderr)
sys.exit(1 if bad else 0)'
    checked $? "config: flows" || true
}

sections=("$@")
[ "${#sections[@]}" -gt 0 ] || sections=(go python web shell config)
for s in "${sections[@]}"; do
    case "${s}" in
        go|python|web|tauri|shell|config) "run_${s}" ;;
        *) echo "unknown section: ${s}" >&2; exit 2 ;;
    esac
done

if [ "${#FAILED[@]}" -gt 0 ]; then
    printf '\nCI failed: %s\n' "${FAILED[*]}" >&2
    exit 1
fi
printf '\nCI passed: %s\n' "${sections[*]}"
if [ "${#SKIPPED[@]}" -gt 0 ]; then
    printf 'WARNING: checks skipped (tool not installed): %s\n' "${SKIPPED[*]}" >&2
fi

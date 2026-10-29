package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// fakeCLI mimics guardian.py closely enough for the hub: canned JSON for config commands,
// exit 2 for inputs containing BAD, and --debate modes selected by the task text.
const fakeCLI = `#!/bin/sh
dir=$(dirname "$0")
printf '%s\n' "$@" > "$dir/last-args.txt"
mode=""; rf=""; thread=""
while [ $# -gt 0 ]; do
  case "$1" in
    --personas) mode=personas ;;
    --teams) mode=teams ;;
    --workers) mode=workers ;;
    --projects) mode=projects ;;
    --project-sync) mode=sync; shift; id="$1" ;;
    --groups) mode=groups ;;
    --command-sets) mode=commandsets ;;
    --persona-save|--team-save|--project-save|--group-save|--group-preview|--command-set-save) mode=save ;;
    --persona-delete|--team-delete|--project-delete|--group-delete|--command-set-delete|--command-set-confirm) mode=delete; shift; id="$1" ;;
    --debate=*) mode=debate; task="${1#--debate=}" ;;
    --result-file) shift; rf="$1" ;;
    --thread) shift; thread="$1" ;;
    --actions) mode=actions ;;
    --show-action) mode=show; shift; id="$1" ;;
    --run-action) mode=run; shift; id="$1" ;;
    --reject-action) mode=reject; shift; id="$1" ;;
    --plans) mode=plans ;;
    --plan-show) mode=planshow; shift; id="$1" ;;
    --plan-run) mode=planrun; shift; id="$1" ;;
    --plan-reject) mode=planreject; shift; id="$1" ;;
    --fail-stale-action) mode=failstale; shift; id="$1" ;;
    --jev-recheck) mode=jev; shift; id="$1" ;;
    --close-thread) mode=close; shift; id="$1" ;;
    --closed-threads) mode=closed ;;
    --published-threads) mode=published ;;
    --publish-thread) mode=publish; shift; id="$1" ;;
    --workspace-reset) mode=wsreset; shift; id="$1" ;;
    --publications) mode=pubs; shift; id="$1" ;;
    --artifact-convert) mode=aconvert; shift; id="$1" ;;
    --report-keep) mode=rkeep ;;
    --plan) plan=1 ;;
    --branch=*) branch="${1#--branch=}" ;;
  esac
  shift
done
case "$mode" in
  personas) echo '{"personas":[{"id":"p1","source":"builtin"}],"custom_dir":"/x"}' ;;
  teams) echo '{"teams":[{"id":"t1","source":"builtin"}],"custom_dir":"/x","catalog":{"max_rounds":10}}' ;;
  workers) echo '{"protocol":2,"code_version":"abc","workers":[{"id":"node-1","enabled":true,"state":"up"}]}' ;;
  projects) echo '{"projects":[{"id":"server-guardian","source":"builtin","errors":[]}],"custom_dir":"/x"}' ;;
  commandsets) echo '{"ok":true,"sets":[{"id":"s1","source":"builtin"}],"stale":[]}' ;;
  groups) printf '{"groups":[{"id":"g1","members":["a"]}],"ungrouped":[],"servers_file":"%s"}\n' "$GUARDIAN_SERVERS_FILE" ;;
  sync)
    if [ "$id" = "missing" ]; then echo '{"ok":false,"error":"no project missing"}'; exit 2; fi
    printf '{"ok":true,"results":[{"id":"%s","ok":true,"action":"updated"}]}\n' "$id" ;;
  save)
    body=$(cat)
    case "$body" in
      *BAD*) echo '{"ok":false,"error":"bad"}'; exit 2 ;;
    esac
    printf '{"ok":true,"id":"saved","got":%s}\n' "$body" ;;
  delete)
    if [ "$id" = "bad-id" ]; then echo '{"ok":false,"error":"bad"}'; exit 2; fi
    if [ "$id" = "crash" ]; then echo "boom" >&2; exit 1; fi
    if [ "$id" = "missing" ]; then echo '{"ok":false,"error":"no group missing"}'; exit 3; fi
    printf '{"ok":true,"id":"%s"}\n' "$id" ;;
  aconvert)
    case "$id" in
      invalid.md) echo '{"error":"invalid.md cannot be converted: too big"}'; exit 2 ;;
      dup.md) echo '{"error":"dup.docx already exists in artifact@v1"}'; exit 4 ;;
      crash.md) echo "boom" >&2; exit 1 ;;
    esac
    printf '{"ok":true,"file":{"path":"%s"},"source":"%s","source_sha256":"%s","result_sha256":"%s","bytes":10}\n' "${id%.md}.docx" "$id" aaaa bbbb ;;
  rkeep)
    case "$thread" in
      *_inval0) echo '{"error":"report contains raw HTML"}'; exit 2 ;;
      *_dup000) echo '{"error":"v1 already holds this report"}'; exit 4 ;;
      *_crash0) echo "boom" >&2; exit 1 ;;
    esac
    printf '{"ok":true,"version":1,"file":{"path":"report.md"},"sha256":"cccc","bytes":10,"warnings":[]}\n' ;;
  actions) echo '{"actions":[{"id":1,"status":"PROPOSED"}]}' ;;
  show)
    if [ "$id" = "3" ]; then echo '{"ok":false,"error":"action 3 not found"}'; exit 3; fi
    printf '{"action":{"id":%s}}\n' "$id" ;;
  run)
    case "$id" in
      4) echo '{"ok":false,"error":"action 4 is EXECUTED, only PROPOSED can run"}'; exit 4 ;;
      7|8) sleep 1 ;;
    esac
    touch "$dir/ran-$id.marker"
    printf '{"ok":true,"action":{"id":%s,"status":"EXECUTED"},"result":{}}\n' "$id" ;;
  plans) echo '{"plans":[{"plan_id":"plan_a_r1","status":"PROPOSED"}]}' ;;
  planshow)
    if [ "$id" = "plan_missing" ]; then echo '{"ok":false,"error":"plan plan_missing not found"}'; exit 3; fi
    printf '{"plan":{"plan_id":"%s"}}\n' "$id" ;;
  planrun)
    case "$id" in
      plan_done) echo '{"ok":false,"error":"plan plan_done has nothing left to run"}'; exit 4 ;;
      plan_slow*) sleep 2 ;;
    esac
    touch "$dir/planran-$id.marker"
    printf '{"ok":true,"outcome":"EXECUTED","plan":{"plan_id":"%s"}}\n' "$id" ;;
  planreject) printf '{"ok":true,"rejected":2,"plan":{"plan_id":"%s"}}\n' "$id" ;;
  reject) printf '{"ok":true,"action":{"id":%s,"status":"REJECTED"}}\n' "$id" ;;
  failstale)
    case "$id" in
      3) echo '{"ok":false,"error":"action 3 not found"}'; exit 3 ;;
      4) echo '{"ok":false,"error":"action 4 is PROPOSED, only RUNNING can be marked failed"}'; exit 4 ;;
    esac
    printf '{"ok":true,"action":{"id":%s,"status":"FAILED"}}\n' "$id" ;;
  jev)
    if [ "$id" = "thread_missing" ]; then echo '{"ok":false,"error":"thread not found"}'; exit 3; fi
    printf '{"ok":true,"gate":"PASSED","released":[1],"review":{"thread":"%s"}}\n' "$id" ;;
  closed)
    echo "x" >> "$dir/closed-calls"
    ids=$(sed 's/.*/"&"/' "$dir/closed.list" 2>/dev/null | paste -sd, -)
    printf '{"ok":true,"threads":[%s]}\n' "$ids" ;;
  published)
    ids=$(sed 's/.*/"&"/' "$dir/published.list" 2>/dev/null | paste -sd, -)
    printf '{"ok":true,"threads":[%s]}\n' "$ids" ;;
  close)
    if [ "$id" = "thread_missing" ]; then echo '{"ok":false,"error":"thread thread_missing not found"}'; exit 3; fi
    echo "$id" >> "$dir/closed.list"
    printf '{"ok":true,"thread_id":"%s","status":"CLOSED","previous_status":"DISPUTED","already_closed":false,"closed_at":"2026-01-02 03:04:05"}\n' "$id" ;;
  publish)
    case "$plan" in
      1)
        case "$id" in
          thread_missing) echo '{"ok":false,"error":"thread thread_missing not found"}'; exit 3 ;;
          thread_refused) echo '{"ok":false,"error":"workspace is dirty"}'; exit 4 ;;
        esac
        printf '{"ok":true,"plan":{"thread_id":"%s","project_id":"p","branch":"%s","remote":"https://x/y.git","base_sha":"abc","files":["a.go"],"subject":"fix: x","retry":false}}\n' "$id" "${branch:-guardian/$id}"
        exit 0 ;;
    esac
    case "$id" in
      thread_missing) echo '{"ok":false,"error":"thread thread_missing not found"}'; exit 3 ;;
      thread_refused) echo '{"ok":false,"error":"the project test failed","tests":{"exit_code":1}}'; exit 4 ;;
      thread_pushfail) echo '{"ok":false,"status":"FAILED","stderr":"remote rejected"}'; exit 0 ;;
    esac
    printf '{"ok":true,"status":"PUSHED","thread_id":"%s","branch":"%s","commit_sha":"abc123"}\n' "$id" "${branch:-guardian/$id}" ;;
  wsreset)
    if [ "$id" = "missing" ]; then echo '{"ok":false,"error":"no project missing"}'; exit 3; fi
    if [ "$id" = "busy" ]; then echo '{"ok":false,"error":"another publish or project sync is running in this workspace"}'; exit 4; fi
    if [ "$plan" = "1" ]; then
      printf '{"ok":true,"project_id":"%s","workspace":"/w","discard":[{"path":"a.go","state":"modified"}],"keep":[{"path":"b.go","action_id":7}]}\n' "$id"
    else
      echo '{"ok":true,"discarded":["a.go"]}'
    fi ;;
  pubs)
    if [ "$id" = "thread_missing" ]; then echo '{"ok":false,"error":"thread thread_missing not found"}'; exit 3; fi
    printf '{"publications":[{"id":2,"branch":"guardian/%s","commit_sha":"abc","remote":"r","status":"PUSHED","error":null,"actor":"op","created_at":"2026-01-01 00:00:00"}]}\n' "$id" ;;
  debate)
    cp "$dir/last-args.txt" "$dir/debate-args.txt"
    echo "round 1 starting"
    echo "round 1 done"
    case "$task" in
      slow*) sleep 30 & echo $! > "$dir/child.pid"; wait ;;
      fail2*) echo "unknown team" >&2; exit 2 ;;
      fail1*) echo "kaboom" >&2; exit 1 ;;
      nores*) exit 0 ;;
    esac
    t="${thread:-thread_new1}"
    printf '{"thread_id":"%s","team":"cross-review","consensus_reached":true,"final_decision":"ok","transcript":[]}\n' "$t" > "$rf"
    exit 0 ;;
esac
`

type testHub struct {
	hub *debateHub
	h   http.Handler
	dir string
	cli string
}

func newTestHub(t *testing.T, mutate func(*debateConfig)) *testHub {
	t.Helper()
	dir := t.TempDir()
	cli := filepath.Join(dir, "fake-guardian.sh")
	if err := os.WriteFile(cli, []byte(fakeCLI), 0o755); err != nil {
		t.Fatal(err)
	}
	cfg := debateConfig{
		CLI:         cli,
		Python:      "sh",
		DataDir:     filepath.Join(dir, "data"),
		MaxRunning:  2,
		JobTimeout:  time.Minute,
		ConfigLimit: 10 * time.Second,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	hub := newDebateHub(cfg)
	mux := http.NewServeMux()
	hub.register(mux)
	return &testHub{hub: hub, h: mux, dir: dir, cli: cli}
}

func (th *testHub) call(method, target, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	rec := httptest.NewRecorder()
	th.h.ServeHTTP(rec, req)
	return rec
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) map[string]interface{} {
	t.Helper()
	var m map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &m); err != nil {
		t.Fatalf("response is not a JSON object: %q (%v)", rec.Body.String(), err)
	}
	return m
}

func (th *testHub) startJob(t *testing.T, task string) map[string]interface{} {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"task": task})
	rec := th.call("POST", "/api/debates", string(body))
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start debate: status %d body %s", rec.Code, rec.Body.String())
	}
	return decode(t, rec)
}

// waitStatus polls GET /api/debates/{id} until the status differs from "running".
func (th *testHub) waitDone(t *testing.T, id string) map[string]interface{} {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		rec := th.call("GET", "/api/debates/"+id, "")
		m := decode(t, rec)
		if m["status"] != "running" {
			return m
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("job %s still running after timeout", id)
	return nil
}

func TestDebateUnavailableWithoutCLI(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.CLI = filepath.Join(t.TempDir(), "missing.py") })
	routes := []struct{ method, path, body string }{
		{"GET", "/api/personas", ""},
		{"POST", "/api/personas", `{"id":"x"}`},
		{"DELETE", "/api/personas/x", ""},
		{"GET", "/api/teams", ""},
		{"GET", "/api/workers", ""},
		{"POST", "/api/teams", `{"id":"x"}`},
		{"DELETE", "/api/teams/x", ""},
		{"POST", "/api/debates", `{"task":"hi"}`},
	}
	for _, rt := range routes {
		rec := th.call(rt.method, rt.path, rt.body)
		if rec.Code != http.StatusServiceUnavailable {
			t.Errorf("%s %s: status %d, want 503", rt.method, rt.path, rec.Code)
			continue
		}
		if got := decode(t, rec)["error"]; got != "debate engine is not available on this host" {
			t.Errorf("%s %s: error %v", rt.method, rt.path, got)
		}
	}
	// Listing, reading and cancelling jobs only touch the job directory, so they keep working
	// when the CLI is missing or moved.
	readOnly := []struct {
		method, path string
		want         int
	}{
		{"GET", "/api/debates", http.StatusOK},
		{"GET", "/api/debates/dbt_20261004_123456_ab12cd", http.StatusNotFound},
		{"DELETE", "/api/debates/dbt_20261004_123456_ab12cd", http.StatusNotFound},
	}
	for _, rt := range readOnly {
		if rec := th.call(rt.method, rt.path, ""); rec.Code != rt.want {
			t.Errorf("%s %s without CLI: status %d, want %d", rt.method, rt.path, rec.Code, rt.want)
		}
	}
}

func TestConfigPassthrough(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/personas", "")
	if rec.Code != 200 {
		t.Fatalf("personas: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["custom_dir"] != "/x" || m["personas"] == nil {
		t.Errorf("unexpected personas output: %v", m)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("content-type %q", ct)
	}
	rec = th.call("GET", "/api/teams", "")
	if rec.Code != 200 || decode(t, rec)["catalog"] == nil {
		t.Errorf("teams: %d %s", rec.Code, rec.Body.String())
	}
	args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if !strings.Contains(string(args), "--teams") || !strings.Contains(string(args), "--json") {
		t.Errorf("CLI args: %q", args)
	}
}

func TestWorkersRouteIsReadOnlyPassthrough(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/workers", "")
	if rec.Code != 200 {
		t.Fatalf("workers: %d %s", rec.Code, rec.Body.String())
	}
	workers, _ := decode(t, rec)["workers"].([]any)
	if len(workers) != 1 {
		t.Errorf("unexpected workers output: %s", rec.Body.String())
	}
	args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if !strings.Contains(string(args), "--workers") || !strings.Contains(string(args), "--json") {
		t.Errorf("CLI args: %q", args)
	}
	for _, method := range []string{"POST", "PUT", "DELETE"} {
		if rec := th.call(method, "/api/workers", `{}`); rec.Code != http.StatusMethodNotAllowed && rec.Code != http.StatusNotFound {
			t.Errorf("%s /api/workers: status %d, want 405 or 404", method, rec.Code)
		}
	}
}

func TestCommandSetRoutes(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/command-sets", "")
	if rec.Code != 200 || decode(t, rec)["sets"] == nil {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	rec = th.call("POST", "/api/command-sets", `{"id":"mine"}`)
	if rec.Code != 200 {
		t.Fatalf("save: %d %s", rec.Code, rec.Body.String())
	}
	args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if !strings.Contains(string(args), "--command-set-save") {
		t.Errorf("CLI args: %q", args)
	}
	if rec = th.call("POST", "/api/command-sets", `{"id":"BAD"}`); rec.Code != 400 {
		t.Errorf("bad save: %d %s", rec.Code, rec.Body.String())
	}
	rec = th.call("POST", "/api/command-sets/mine/confirm", "")
	if rec.Code != 200 || decode(t, rec)["id"] != "mine" {
		t.Errorf("confirm: %d %s", rec.Code, rec.Body.String())
	}
	args, _ = os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if !strings.Contains(string(args), "--command-set-confirm") {
		t.Errorf("CLI args: %q", args)
	}
	if rec = th.call("POST", "/api/command-sets/missing/confirm", ""); rec.Code != 404 {
		t.Errorf("confirm unknown: %d %s", rec.Code, rec.Body.String())
	}
	if rec = th.call("POST", "/api/command-sets/BAD%20ID/confirm", ""); rec.Code != 400 && rec.Code != 404 {
		t.Errorf("confirm bad id: %d %s", rec.Code, rec.Body.String())
	}
	if rec = th.call("DELETE", "/api/command-sets/mine", ""); rec.Code != 200 || decode(t, rec)["id"] != "mine" {
		t.Errorf("delete: %d %s", rec.Code, rec.Body.String())
	}
}

func TestConfigSaveAndDelete(t *testing.T) {
	th := newTestHub(t, nil)

	rec := th.call("POST", "/api/teams", `{"id":"mine"}`)
	if rec.Code != 200 {
		t.Fatalf("save: %d %s", rec.Code, rec.Body.String())
	}
	got, _ := decode(t, rec)["got"].(map[string]interface{})
	if got["id"] != "mine" {
		t.Errorf("stdin did not reach CLI: %s", rec.Body.String())
	}

	// exit 2 -> 400 with the CLI error
	rec = th.call("POST", "/api/personas", `{"id":"BAD"}`)
	if rec.Code != 400 || decode(t, rec)["error"] != "bad" {
		t.Errorf("bad save: %d %s", rec.Code, rec.Body.String())
	}

	if rec = th.call("DELETE", "/api/teams/mine", ""); rec.Code != 200 || decode(t, rec)["id"] != "mine" {
		t.Errorf("delete: %d %s", rec.Code, rec.Body.String())
	}
	if rec = th.call("DELETE", "/api/personas/bad-id", ""); rec.Code != 400 || decode(t, rec)["error"] != "bad" {
		t.Errorf("delete exit 2: %d %s", rec.Code, rec.Body.String())
	}
	// An unknown id is exit 3 -> 404 for every kind; a builtin one stays exit 2 -> 400 (above).
	for _, kind := range []string{"personas", "teams", "projects", "groups", "command-sets"} {
		if rec = th.call("DELETE", "/api/"+kind+"/missing", ""); rec.Code != 404 || !strings.Contains(decode(t, rec)["error"].(string), "missing") {
			t.Errorf("delete unknown %s: %d %s", kind, rec.Code, rec.Body.String())
		}
	}

	// other exit codes -> 500 including stderr
	rec = th.call("DELETE", "/api/teams/crash", "")
	if rec.Code != 500 || !strings.Contains(decode(t, rec)["error"].(string), "boom") {
		t.Errorf("crash: %d %s", rec.Code, rec.Body.String())
	}

	// id validation happens before the CLI is run
	for _, id := range []string{"UPPER", "-lead", "a%20b", strings.Repeat("a", 65), "x;rm"} {
		if rec = th.call("DELETE", "/api/teams/"+id, ""); rec.Code != 400 {
			t.Errorf("delete %q: status %d, want 400", id, rec.Code)
		}
	}

	// body limit and empty body
	if rec = th.call("POST", "/api/teams", strings.Repeat("a", 64<<10+1)); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body: %d", rec.Code)
	}
	if rec = th.call("POST", "/api/teams", "  "); rec.Code != 400 {
		t.Errorf("empty body: %d", rec.Code)
	}
}

func TestStartDebateValidation(t *testing.T) {
	th := newTestHub(t, nil)
	cases := []struct {
		name, body string
	}{
		{"empty body", ``},
		{"not json", `nope`},
		{"missing task", `{}`},
		{"blank task", `{"task":"   "}`},
		{"task too long", `{"task":"` + strings.Repeat("a", 4001) + `"}`},
		{"nul in task", `{"task":"a\u0000b"}`},
		{"bad team", `{"task":"x","team":"Bad Team"}`},
		{"bad thread", `{"task":"x","thread_id":"abc"}`},
		{"thread traversal", `{"task":"x","thread_id":"thread_../x"}`},
		{"rounds zero", `{"task":"x","rounds":0}`},
		{"rounds too big", `{"task":"x","rounds":11}`},
		{"body too large", `{"task":"x","pad":"` + strings.Repeat("a", 16<<10) + `"}`},
	}
	for _, c := range cases {
		rec := th.call("POST", "/api/debates", c.body)
		want := 400
		if c.name == "body too large" {
			want = http.StatusRequestEntityTooLarge
		}
		if rec.Code != want {
			t.Errorf("%s: status %d, want %d (%s)", c.name, rec.Code, want, rec.Body.String())
		}
	}
	// exactly 4000 multibyte characters is accepted
	task := strings.Repeat("한", 4000)
	body, _ := json.Marshal(map[string]string{"task": task})
	if rec := th.call("POST", "/api/debates", string(body)); rec.Code != http.StatusAccepted {
		t.Errorf("4000 chars: %d %s", rec.Code, rec.Body.String())
	} else {
		th.waitDone(t, decode(t, rec)["id"].(string))
	}
}

func TestStartDebateContextValidation(t *testing.T) {
	th := newTestHub(t, nil)
	paths := func(ps ...string) string {
		b, _ := json.Marshal(map[string]interface{}{"task": "x", "context_paths": ps})
		return string(b)
	}
	ref := func(r string) string {
		b, _ := json.Marshal(map[string]interface{}{"task": "x", "context_ref": r})
		return string(b)
	}
	many := make([]string, 21)
	for i := range many {
		many[i] = "a/*.go"
	}
	cases := []struct{ name, body, field string }{
		{"21 paths", paths(many...), "context_paths"},
		{"empty path", paths("a", ""), "context_paths[1]"},
		{"201 runes", paths(strings.Repeat("한", 201)), "context_paths[0]"},
		{"newline", paths("a\nb"), "context_paths[0]"},
		{"carriage return", paths("a\rb"), "context_paths[0]"},
		{"nul", paths("a\x00b"), "context_paths[0]"},
		{"leading dash", paths("-rf"), "context_paths[0]"},
		{"bad ref", ref("a b"), "context_ref"},
		{"ref semicolon", ref("HEAD;ls"), "context_ref"},
		{"ref leading dash", ref("-HEAD"), "context_ref"},
		{"ref too long", ref(strings.Repeat("a", 101)), "context_ref"},
		{"ref trailing newline", ref("HEAD\n"), "context_ref"},
	}
	for _, c := range cases {
		rec := th.call("POST", "/api/debates", c.body)
		if rec.Code != 400 {
			t.Errorf("%s: status %d, want 400 (%s)", c.name, rec.Code, rec.Body.String())
			continue
		}
		if msg, _ := decode(t, rec)["error"].(string); !strings.Contains(msg, c.field) {
			t.Errorf("%s: error %q does not name %s", c.name, msg, c.field)
		}
	}
	// boundaries are accepted: 20 paths, 200-rune path, 100-char ref
	edge := make([]string, 20)
	for i := range edge {
		edge[i] = "p"
	}
	edge[0] = strings.Repeat("한", 200)
	b, _ := json.Marshal(map[string]interface{}{"task": "x", "context_paths": edge, "context_ref": strings.Repeat("a", 100)})
	rec := th.call("POST", "/api/debates", string(b))
	if rec.Code != http.StatusAccepted {
		t.Fatalf("boundary request: %d %s", rec.Code, rec.Body.String())
	}
	th.waitDone(t, decode(t, rec)["id"].(string))
}

func TestDebateContextArgvAndSummary(t *testing.T) {
	th := newTestHub(t, nil)
	body := `{"task":"review","context_paths":["apps/agent/*.go","docs/**/*.md","a b"],"context_ref":"origin/main~1"}`
	rec := th.call("POST", "/api/debates", body)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	id := decode(t, rec)["id"].(string)
	th.waitDone(t, id)
	data, err := os.ReadFile(filepath.Join(th.dir, "debate-args.txt"))
	if err != nil {
		t.Fatal(err)
	}
	args := strings.Split(strings.TrimSpace(string(data)), "\n")
	want := []string{"--context-path=apps/agent/*.go", "--context-path=docs/**/*.md", "--context-path=a b", "--context-ref=origin/main~1"}
	var got []string
	for _, a := range args {
		if strings.HasPrefix(a, "--context-") {
			got = append(got, a)
		}
		if a == "--context-path" || a == "--context-ref" {
			t.Errorf("flag and value passed as separate argv entries: %v", args)
		}
	}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("context argv = %v, want %v", got, want)
	}

	check := func(m map[string]interface{}, where string) {
		t.Helper()
		ps, _ := m["context_paths"].([]interface{})
		if len(ps) != 3 || ps[0] != "apps/agent/*.go" || ps[1] != "docs/**/*.md" || ps[2] != "a b" {
			t.Errorf("%s: context_paths = %v", where, m["context_paths"])
		}
		if m["context_ref"] != "origin/main~1" {
			t.Errorf("%s: context_ref = %v", where, m["context_ref"])
		}
	}
	check(decode(t, th.call("GET", "/api/debates/"+id, "")), "detail")
	list := decode(t, th.call("GET", "/api/debates", ""))["debates"].([]interface{})
	if len(list) != 1 {
		t.Fatalf("list has %d jobs", len(list))
	}
	check(list[0].(map[string]interface{}), "list")
}

func TestDebateWithoutContextOmitsFlagsAndFields(t *testing.T) {
	th := newTestHub(t, nil)
	job := th.startJob(t, "plain")
	id := job["id"].(string)
	th.waitDone(t, id)
	data, _ := os.ReadFile(filepath.Join(th.dir, "debate-args.txt"))
	if strings.Contains(string(data), "--context-") {
		t.Errorf("unexpected context flags: %s", data)
	}
	m := decode(t, th.call("GET", "/api/debates/"+id, ""))
	if _, ok := m["context_paths"]; ok {
		t.Errorf("context_paths should be omitted: %v", m)
	}
	if _, ok := m["context_ref"]; ok {
		t.Errorf("context_ref should be omitted: %v", m)
	}
}

func TestDebateCompletes(t *testing.T) {
	th := newTestHub(t, nil)
	body := `{"task":"--dangerous; $(touch /tmp/pwned)","team":"cross-review","rounds":3}`
	rec := th.call("POST", "/api/debates", body)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	job := decode(t, rec)
	id := job["id"].(string)
	if !jobIDPattern.MatchString(id) || job["status"] != "running" || job["team"] != "cross-review" || job["rounds"] != float64(3) {
		t.Errorf("unexpected summary: %v", job)
	}

	done := th.waitDone(t, id)
	if done["status"] != "completed" || done["exit_code"] != float64(0) || done["thread_id"] != "thread_new1" {
		t.Fatalf("unexpected final state: %v", done)
	}
	if done["finished_at"] == nil {
		t.Error("finished_at not set")
	}
	if !strings.Contains(done["log"].(string), "round 1 done") {
		t.Errorf("log missing progress: %q", done["log"])
	}
	res, _ := done["result"].(map[string]interface{})
	if res["thread_id"] != "thread_new1" || res["consensus_reached"] != true {
		t.Errorf("result: %v", done["result"])
	}

	// the task reached the CLI as a single argv entry, never via a shell
	args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if !strings.Contains(string(args), "--debate=--dangerous; $(touch /tmp/pwned)\n") {
		t.Errorf("task not passed as one argument: %q", args)
	}
	if !strings.Contains(string(args), "--team\ncross-review\n") || !strings.Contains(string(args), "--rounds\n3\n") {
		t.Errorf("flags missing: %q", args)
	}

	// meta.json and friends are on disk
	jobDir := filepath.Join(th.dir, "data", "debate-jobs", id)
	for _, f := range []string{"meta.json", "output.log", "result.json"} {
		if _, err := os.Stat(filepath.Join(jobDir, f)); err != nil {
			t.Errorf("%s missing: %v", f, err)
		}
	}
	if m, _ := filepath.Glob(filepath.Join(jobDir, ".meta-*")); len(m) != 0 {
		t.Errorf("temp files left behind: %v", m)
	}

	// the job shows up in the list
	list := decode(t, th.call("GET", "/api/debates", ""))["debates"].([]interface{})
	if len(list) != 1 || list[0].(map[string]interface{})["id"] != id {
		t.Errorf("list: %v", list)
	}
	if _, has := list[0].(map[string]interface{})["log"]; has {
		t.Error("list entries must not carry the log")
	}
}

func TestDebateContinueThreadKeepsRequestedID(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/debates", `{"task":"again","thread_id":"thread_old_9"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	job := decode(t, rec)
	if job["thread_id"] != "thread_old_9" {
		t.Errorf("thread_id at start: %v", job["thread_id"])
	}
	if done := th.waitDone(t, job["id"].(string)); done["status"] != "completed" || done["thread_id"] != "thread_old_9" {
		t.Errorf("final: %v", done)
	}
}

func TestDebateFailures(t *testing.T) {
	th := newTestHub(t, nil)
	cases := []struct {
		task, wantErr string
		exit          float64
	}{
		{"fail2 please", "validation failed", 2},
		{"fail1 please", "debate exited with code 1", 1},
		{"nores please", "without a valid result", 0},
	}
	for _, c := range cases {
		done := th.waitDone(t, th.startJob(t, c.task)["id"].(string))
		if done["status"] != "failed" || done["exit_code"] != c.exit || !strings.Contains(done["error"].(string), c.wantErr) {
			t.Errorf("%s: %v", c.task, done)
		}
		if c.exit != 0 && !strings.Contains(done["error"].(string), "round 1 done") {
			t.Errorf("%s: error should include the log tail: %v", c.task, done["error"])
		}
		if done["result"] != nil {
			t.Errorf("%s: result should be null: %v", c.task, done["result"])
		}
	}
}

func TestDebateCancelKillsProcessGroup(t *testing.T) {
	th := newTestHub(t, nil)
	id := th.startJob(t, "slow one")["id"].(string)

	var childPID int
	waitUntil(t, "grandchild started", func() bool {
		data, err := os.ReadFile(filepath.Join(th.dir, "child.pid"))
		if err != nil {
			return false
		}
		childPID, err = strconv.Atoi(strings.TrimSpace(string(data)))
		return err == nil
	})

	rec := th.call("DELETE", "/api/debates/"+id, "")
	if rec.Code != 200 || decode(t, rec)["status"] != "cancelled" {
		t.Fatalf("cancel: %d %s", rec.Code, rec.Body.String())
	}
	waitUntil(t, "grandchild killed", func() bool { return syscall.Kill(childPID, 0) != nil })

	waitUntil(t, "exit code recorded", func() bool {
		return decode(t, th.call("GET", "/api/debates/"+id, ""))["exit_code"] != nil
	})
	m := decode(t, th.call("GET", "/api/debates/"+id, ""))
	if m["status"] != "cancelled" || m["finished_at"] == nil {
		t.Errorf("after cancel: %v", m)
	}

	// finished job cannot be cancelled again; unknown job is 404; bad id is 400
	if rec = th.call("DELETE", "/api/debates/"+id, ""); rec.Code != http.StatusConflict {
		t.Errorf("second cancel: %d", rec.Code)
	}
	if rec = th.call("DELETE", "/api/debates/dbt_20200101_000000_aaaaaa", ""); rec.Code != 404 {
		t.Errorf("unknown: %d", rec.Code)
	}
	if rec = th.call("DELETE", "/api/debates/..%2f..%2fetc", ""); rec.Code != 400 && rec.Code != 404 {
		t.Errorf("traversal: %d", rec.Code)
	}
}

func TestDebateCancelFinishedJobIs409(t *testing.T) {
	th := newTestHub(t, nil)
	id := th.startJob(t, "quick")["id"].(string)
	th.waitDone(t, id)
	if rec := th.call("DELETE", "/api/debates/"+id, ""); rec.Code != http.StatusConflict {
		t.Errorf("status %d", rec.Code)
	}
}

func TestDebateConcurrencyLimit(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.MaxRunning = 1 })
	first := th.startJob(t, "slow a")["id"].(string)

	rec := th.call("POST", "/api/debates", `{"task":"second"}`)
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("second start: %d %s", rec.Code, rec.Body.String())
	}
	if th.call("DELETE", "/api/debates/"+first, "").Code != 200 {
		t.Fatal("cancel failed")
	}
	// the slot is released once the process is gone
	waitUntil(t, "slot released", func() bool {
		th.hub.mu.Lock()
		defer th.hub.mu.Unlock()
		return len(th.hub.runs) == 0
	})
	if rec = th.call("POST", "/api/debates", `{"task":"third"}`); rec.Code != http.StatusAccepted {
		t.Errorf("third start: %d %s", rec.Code, rec.Body.String())
	} else {
		th.waitDone(t, decode(t, rec)["id"].(string))
	}
}

func TestStartDebateRefusedWhileThreadHasARunningJob(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.MaxRunning = 3 })
	first := startedID(t, th.call("POST", "/api/debates", `{"task":"slow a","thread_id":"thread_abc1"}`))

	rec := th.call("POST", "/api/debates", `{"task":"again","thread_id":"thread_abc1"}`)
	if rec.Code != http.StatusConflict || !strings.Contains(decode(t, rec)["error"].(string), "already running for this thread") {
		t.Fatalf("same thread: %d %s", rec.Code, rec.Body.String())
	}
	// Another thread and a new (thread-less) debate are not affected, and the refusal reserved no slot.
	other := startedID(t, th.call("POST", "/api/debates", `{"task":"slow b","thread_id":"thread_other"}`))
	fresh := startedID(t, th.call("POST", "/api/debates", `{"task":"slow c"}`))
	th.hub.mu.Lock()
	running := len(th.hub.runs)
	th.hub.mu.Unlock()
	if running != 3 {
		t.Errorf("runs = %d, want 3", running)
	}
	// Once the first job is gone the thread can be continued again.
	if th.call("DELETE", "/api/debates/"+first, "").Code != 200 {
		t.Fatal("cancel failed")
	}
	waitUntil(t, "first job released its slot", func() bool {
		th.hub.mu.Lock()
		defer th.hub.mu.Unlock()
		_, live := th.hub.runs[first]
		return !live
	})
	again := startedID(t, th.call("POST", "/api/debates", `{"task":"slow d","thread_id":"thread_abc1"}`))
	for _, id := range []string{other, fresh, again} {
		th.call("DELETE", "/api/debates/"+id, "")
	}
	// Let the cancelled jobs finish writing their meta before the temp dir is removed.
	waitUntil(t, "all jobs finished", func() bool {
		th.hub.mu.Lock()
		defer th.hub.mu.Unlock()
		return len(th.hub.runs) == 0
	})
}

// startedID returns the job id of a 202 start response.
func startedID(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	return decode(t, rec)["id"].(string)
}

func TestDebateTimeout(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.JobTimeout = 300 * time.Millisecond })
	done := th.waitDone(t, th.startJob(t, "slow forever")["id"].(string))
	if done["status"] != "failed" || !strings.Contains(done["error"].(string), "timed out") {
		t.Errorf("timeout: %v", done)
	}
}

func TestRunningJobsBecomeInterruptedOnStartup(t *testing.T) {
	th := newTestHub(t, nil)
	jobs := filepath.Join(th.dir, "data", "debate-jobs")
	writeMeta := func(id, status string) {
		dir := filepath.Join(jobs, id)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		meta := fmt.Sprintf(`{"id":%q,"task":"t","team":"","thread_id":"","rounds":0,"status":%q,"started_at":"2026-10-04T12:34:56Z","finished_at":null,"exit_code":null,"error":""}`, id, status)
		if err := os.WriteFile(filepath.Join(dir, "meta.json"), []byte(meta), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeMeta("dbt_20261004_123456_ab12cd", "running")
	writeMeta("dbt_20261004_123457_ab12ce", "completed")

	restarted := newTestHub(t, nil)
	restarted.hub.cfg.DataDir = th.hub.cfg.DataDir
	restarted.hub.markInterrupted()
	m := decode(t, restarted.call("GET", "/api/debates/dbt_20261004_123456_ab12cd", ""))
	if m["status"] != "interrupted" || m["finished_at"] == nil {
		t.Errorf("running job: %v", m)
	}
	m = decode(t, restarted.call("GET", "/api/debates/dbt_20261004_123457_ab12ce", ""))
	if m["status"] != "completed" {
		t.Errorf("completed job changed: %v", m)
	}

	// constructing a hub does the same thing
	writeMeta("dbt_20261004_123458_ab12cf", "running")
	h2 := newDebateHub(th.hub.cfg)
	if s, _ := h2.readMeta("dbt_20261004_123458_ab12cf"); s.Status != "interrupted" {
		t.Errorf("startup did not mark job interrupted: %v", s.Status)
	}
}

func TestListDebatesNewestFirstAndCapped(t *testing.T) {
	th := newTestHub(t, nil)
	jobs := filepath.Join(th.dir, "data", "debate-jobs")
	for i := 0; i < 55; i++ {
		id := fmt.Sprintf("dbt_20261004_12%04d_ab12cd", i)
		dir := filepath.Join(jobs, id)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		started := time.Date(2026, 10, 4, 12, 0, i, 0, time.UTC).Format(time.RFC3339)
		meta := fmt.Sprintf(`{"id":%q,"status":"completed","started_at":%q}`, id, started)
		if err := os.WriteFile(filepath.Join(dir, "meta.json"), []byte(meta), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	// junk entries are ignored
	_ = os.MkdirAll(filepath.Join(jobs, "not-a-job"), 0o700)
	_ = os.MkdirAll(filepath.Join(jobs, "dbt_20261004_120099_ab12cd"), 0o700)

	list := decode(t, th.call("GET", "/api/debates", ""))["debates"].([]interface{})
	if len(list) != 50 {
		t.Fatalf("len %d, want 50", len(list))
	}
	if first := list[0].(map[string]interface{})["id"]; first != "dbt_20261004_120054_ab12cd" {
		t.Errorf("first %v", first)
	}
	if last := list[49].(map[string]interface{})["id"]; last != "dbt_20261004_120005_ab12cd" {
		t.Errorf("last %v", last)
	}
}

func TestGetDebateUnknownAndInvalid(t *testing.T) {
	th := newTestHub(t, nil)
	if rec := th.call("GET", "/api/debates/dbt_20200101_000000_aaaaaa", ""); rec.Code != 404 {
		t.Errorf("unknown: %d", rec.Code)
	}
	if rec := th.call("GET", "/api/debates/not-a-job", ""); rec.Code != 400 {
		t.Errorf("invalid: %d", rec.Code)
	}
	if rec := th.call("GET", "/api/debates", ""); rec.Code != 200 || decode(t, rec)["debates"] == nil {
		t.Errorf("empty list should be an array: %s", rec.Body.String())
	}
}

func TestDebateRoutesRequireAuth(t *testing.T) {
	th := newTestHub(t, nil)
	h := withAuth(th.h, testToken)
	for _, rt := range []struct{ method, path string }{
		{"GET", "/api/personas"}, {"GET", "/api/teams"}, {"POST", "/api/debates"},
		{"GET", "/api/debates"}, {"DELETE", "/api/teams/x"},
	} {
		if rec := do(h, rt.method, rt.path, `{"task":"x"}`, nil); rec.Code != http.StatusUnauthorized {
			t.Errorf("%s %s without token: %d", rt.method, rt.path, rec.Code)
		}
	}
	rec := do(h, "GET", "/api/personas", "", func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+testToken) })
	if rec.Code != 200 {
		t.Errorf("with token: %d %s", rec.Code, rec.Body.String())
	}
}

func TestDebateConfigFromEnv(t *testing.T) {
	t.Setenv("GUARDIAN_CLI", "")
	t.Setenv("GUARDIAN_PYTHON", "")
	t.Setenv("GUARDIAN_DATA_DIR", "")
	t.Setenv("GUARDIAN_MAX_DEBATES", "")
	t.Setenv("GUARDIAN_DEBATE_TIMEOUT", "")
	cfg := debateConfigFromEnv()
	if cfg.CLI != "cli/llm-agent/guardian.py" || cfg.Python != "python3" || cfg.MaxRunning != 2 || cfg.JobTimeout != time.Hour {
		t.Errorf("defaults: %+v", cfg)
	}
	if !strings.HasSuffix(cfg.DataDir, filepath.Join(".guardian", "data")) {
		t.Errorf("data dir default: %s", cfg.DataDir)
	}
	t.Setenv("GUARDIAN_MAX_DEBATES", "5")
	t.Setenv("GUARDIAN_DEBATE_TIMEOUT", "90")
	t.Setenv("GUARDIAN_DATA_DIR", "/srv/data")
	cfg = debateConfigFromEnv()
	if cfg.MaxRunning != 5 || cfg.JobTimeout != 90*time.Second || cfg.DataDir != "/srv/data" {
		t.Errorf("overrides: %+v", cfg)
	}
}

func waitUntil(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for: %s", what)
}

func TestRespondConfigExit2WithoutJSONShowsStderr(t *testing.T) {
	// An older guardian.py rejects unknown flags through argparse (exit 2, usage on stderr).
	rec := httptest.NewRecorder()
	(&debateHub{}).respondConfig(rec, 2, nil, "guardian.py: error: unrecognized arguments: --teams", nil)
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "unrecognized arguments: --teams") {
		t.Fatalf("got %d %s", rec.Code, rec.Body.String())
	}
}

// ---- judge actions ----

func (th *testHub) lastArgs(t *testing.T) []string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(strings.TrimSpace(string(data)), "\n")
}

func TestActionsListPassthrough(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/actions", "")
	if rec.Code != 200 || decode(t, rec)["actions"] == nil {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--actions --json" {
		t.Errorf("argv without thread: %q", got)
	}
	rec = th.call("GET", "/api/actions?thread=thread_abc123", "")
	if rec.Code != 200 {
		t.Fatalf("list thread: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--actions thread_abc123 --json" {
		t.Errorf("argv with thread: %q", got)
	}
	for _, bad := range []string{"nope", "thread_", "thread_a%20b", "thread_a;rm"} {
		rec = th.call("GET", "/api/actions?thread="+bad, "")
		if rec.Code != 400 {
			t.Errorf("thread %q: status %d, want 400", bad, rec.Code)
		}
	}
}

func TestActionBadIDs(t *testing.T) {
	th := newTestHub(t, nil)
	for _, id := range []string{"0", "-1", "01", "abc", "99999999999", "2147483648", "+1", "1.5"} {
		for _, rt := range []struct{ method, path, body string }{
			{"GET", "/api/actions/" + id, ""},
			{"POST", "/api/actions/" + id + "/run", `{"confirm_id":1}`},
			{"POST", "/api/actions/" + id + "/reject", ""},
		} {
			if rec := th.call(rt.method, rt.path, rt.body); rec.Code != 400 {
				t.Errorf("%s %s: status %d, want 400", rt.method, rt.path, rec.Code)
			}
		}
	}
	if rec := th.call("GET", "/api/actions/2147483647", ""); rec.Code != 200 {
		t.Errorf("max id: %d %s", rec.Code, rec.Body.String())
	}
}

func TestActionShowAndNotFound(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/actions/5", "")
	if rec.Code != 200 {
		t.Fatalf("show: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--show-action 5 --json" {
		t.Errorf("argv: %q", got)
	}
	rec = th.call("GET", "/api/actions/3", "")
	if rec.Code != 404 || decode(t, rec)["error"] != "action 3 not found" {
		t.Errorf("exit 3: %d %s", rec.Code, rec.Body.String())
	}
}

func TestActionRunSuccess(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/actions/12/run", `{"confirm_id":12}`)
	if rec.Code != 200 {
		t.Fatalf("run: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["ok"] != true {
		t.Errorf("body: %s", rec.Body.String())
	}
	args := th.lastArgs(t)
	joined := strings.Join(args, " ")
	for _, want := range []string{"--run-action 12", "--yes", "--json", "--actor dashboard@192.0.2.1"} {
		if !strings.Contains(joined, want) {
			t.Errorf("argv %q misses %q", joined, want)
		}
	}
}

func TestActionRunConfirmMismatch(t *testing.T) {
	th := newTestHub(t, nil)
	for _, body := range []string{`{"confirm_id":13}`, `{}`, `{"confirm_id":"12"}`, `not json`, ``} {
		rec := th.call("POST", "/api/actions/12/run", body)
		if rec.Code != 400 {
			t.Errorf("body %q: status %d, want 400", body, rec.Code)
		}
	}
	rec := th.call("POST", "/api/actions/12/run", `{"confirm_id":13}`)
	if decode(t, rec)["error"] != "confirm_id must match the action id" {
		t.Errorf("error text: %s", rec.Body.String())
	}
	if _, err := os.Stat(filepath.Join(th.dir, "ran-12.marker")); err == nil {
		t.Error("CLI ran despite confirm mismatch")
	}
	big := `{"confirm_id":12,"pad":"` + strings.Repeat("x", 5000) + `"}`
	if rec := th.call("POST", "/api/actions/12/run", big); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body: %d", rec.Code)
	}
}

func TestActionRunDisabledStillAllowsReject(t *testing.T) {
	for _, v := range []string{"off", "OFF", "Off"} {
		th := newTestHub(t, func(c *debateConfig) { c.UIActions = v })
		rec := th.call("POST", "/api/actions/12/run", `{"confirm_id":12}`)
		if rec.Code != 403 || !strings.Contains(decode(t, rec)["error"].(string), "GUARDIAN_UI_ACTIONS=off") {
			t.Errorf("%s run: %d %s", v, rec.Code, rec.Body.String())
		}
		if _, err := os.Stat(filepath.Join(th.dir, "ran-12.marker")); err == nil {
			t.Error("CLI ran while disabled")
		}
		if rec := th.call("POST", "/api/actions/12/reject", ""); rec.Code != 403 {
			t.Errorf("reject while disabled: %d %s", rec.Code, rec.Body.String())
		}
		if rec := th.call("GET", "/api/actions/12", ""); rec.Code != 200 {
			t.Errorf("show while disabled: %d", rec.Code)
		}
		if rec := th.call("GET", "/api/actions", ""); rec.Code != 200 {
			t.Errorf("list while disabled: %d", rec.Code)
		}
	}
}

func TestActionRejectArgv(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/actions/9/reject", "")
	if rec.Code != 200 {
		t.Fatalf("reject: %d %s", rec.Code, rec.Body.String())
	}
	joined := strings.Join(th.lastArgs(t), " ")
	if !strings.HasPrefix(joined, "--reject-action 9 --json --actor dashboard@192.0.2.1") {
		t.Errorf("argv: %q", joined)
	}
}

func TestActionRunConflictExit4(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/actions/4/run", `{"confirm_id":4}`)
	if rec.Code != 409 || decode(t, rec)["error"] != "action 4 is EXECUTED, only PROPOSED can run" {
		t.Errorf("exit 4: %d %s", rec.Code, rec.Body.String())
	}
}

func TestActionRunDuplicateInFlight(t *testing.T) {
	th := newTestHub(t, nil)
	first := make(chan *httptest.ResponseRecorder, 1)
	go func() { first <- th.call("POST", "/api/actions/7/run", `{"confirm_id":7}`) }()

	deadline := time.Now().Add(5 * time.Second)
	for {
		th.hub.mu.Lock()
		_, busy := th.hub.actionRuns[7]
		th.hub.mu.Unlock()
		if busy {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("first run never registered as in flight")
		}
		time.Sleep(5 * time.Millisecond)
	}
	rec := th.call("POST", "/api/actions/7/run", `{"confirm_id":7}`)
	if rec.Code != 409 || decode(t, rec)["error"] != "action is already running" {
		t.Errorf("duplicate: %d %s", rec.Code, rec.Body.String())
	}
	if rec := <-first; rec.Code != 200 {
		t.Errorf("first run: %d %s", rec.Code, rec.Body.String())
	}
	// the id is released afterwards
	if rec := th.call("POST", "/api/actions/7/run", `{"confirm_id":7}`); rec.Code != 200 {
		t.Errorf("rerun after finish: %d %s", rec.Code, rec.Body.String())
	}
}

func TestActionRunSurvivesClientCancel(t *testing.T) {
	th := newTestHub(t, nil)
	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest("POST", "/api/actions/8/run", strings.NewReader(`{"confirm_id":8}`)).WithContext(ctx)
	rec := httptest.NewRecorder()
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		th.h.ServeHTTP(rec, req)
	}()
	time.Sleep(200 * time.Millisecond) // the fake CLI is sleeping now
	cancel()
	wg.Wait()
	if _, err := os.Stat(filepath.Join(th.dir, "ran-8.marker")); err != nil {
		t.Fatalf("CLI was killed by the client cancel: %v", err)
	}
	if rec.Code != 200 {
		t.Errorf("status %d %s", rec.Code, rec.Body.String())
	}
}

func TestActionRunTimeout(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.ActionTimeout = 200 * time.Millisecond })
	rec := th.call("POST", "/api/actions/7/run", `{"confirm_id":7}`)
	if rec.Code != 500 || !strings.Contains(decode(t, rec)["error"].(string), "timed out") {
		t.Errorf("timeout: %d %s", rec.Code, rec.Body.String())
	}
	th.hub.mu.Lock()
	defer th.hub.mu.Unlock()
	if len(th.hub.actionRuns) != 0 {
		t.Errorf("in-flight set not cleared: %v", th.hub.actionRuns)
	}
}

func TestJevRecheck(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/threads/thread_abc1/jev-recheck", "")
	if rec.Code != 200 || decode(t, rec)["gate"] != "PASSED" {
		t.Fatalf("recheck: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--jev-recheck thread_abc1 --json" {
		t.Errorf("argv: %q", got)
	}
	rec = th.call("POST", "/api/threads/thread_missing/jev-recheck", "")
	if rec.Code != 404 || decode(t, rec)["error"] != "thread not found" {
		t.Errorf("missing: %d %s", rec.Code, rec.Body.String())
	}
	if rec = th.call("POST", "/api/threads/bad/jev-recheck", ""); rec.Code != 400 {
		t.Errorf("bad thread: %d", rec.Code)
	}
}

func TestCloseThread(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/threads/thread_abc1/close", "")
	if got := decode(t, rec); rec.Code != 200 || got["status"] != "CLOSED" || got["thread_id"] != "thread_abc1" {
		t.Fatalf("close: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--close-thread thread_abc1 --json" {
		t.Errorf("argv: %q", got)
	}
	rec = th.call("POST", "/api/threads/thread_missing/close", "")
	if rec.Code != 404 || decode(t, rec)["error"] != "thread thread_missing not found" {
		t.Errorf("missing: %d %s", rec.Code, rec.Body.String())
	}
	for _, bad := range []string{"bad", "thread_", "thread_a-b"} {
		if rec = th.call("POST", "/api/threads/"+bad+"/close", ""); rec.Code != 400 {
			t.Errorf("%s: %d", bad, rec.Code)
		}
	}
}

// writeJobMeta stores a finished job's meta.json the way the hub does.
func (th *testHub) writeJobMeta(t *testing.T, id, thread string, closed bool) {
	t.Helper()
	dir := filepath.Join(th.hub.jobsDir(), id)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	th.hub.mu.Lock()
	defer th.hub.mu.Unlock()
	th.hub.writeMetaLocked(debateSummary{ID: id, Task: "t", Status: "completed", ThreadID: thread,
		StartedAt: now, ThreadClosed: closed})
}

func TestCloseThreadMarksStoredJobs(t *testing.T) {
	th := newTestHub(t, nil)
	th.writeJobMeta(t, "dbt_20260101_000001_aaaaaa", "thread_abc1", false)
	th.writeJobMeta(t, "dbt_20260101_000002_bbbbbb", "thread_other", false)
	if rec := th.call("POST", "/api/threads/thread_abc1/close", ""); rec.Code != 200 {
		t.Fatalf("close: %d %s", rec.Code, rec.Body.String())
	}
	closedOf := func(id string) bool {
		sum, err := th.hub.readMeta(id)
		if err != nil {
			t.Fatal(err)
		}
		return sum.ThreadClosed
	}
	if !closedOf("dbt_20260101_000001_aaaaaa") || closedOf("dbt_20260101_000002_bbbbbb") {
		t.Errorf("only the closed thread's jobs carry thread_closed")
	}
	rec := th.call("GET", "/api/debates", "")
	if !strings.Contains(rec.Body.String(), `"thread_closed":true`) {
		t.Errorf("list lacks thread_closed: %s", rec.Body.String())
	}
	// A job that finishes on the thread reopens it for every earlier job.
	run := &debateRun{summary: debateSummary{ID: "dbt_20260101_000003_cccccc", Status: "running", ThreadID: "thread_abc1", StartedAt: time.Now().UTC()}}
	dir := filepath.Join(th.hub.jobsDir(), run.summary.ID)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "result.json"), []byte(`{"thread_id":"thread_abc1"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	th.hub.mu.Lock()
	th.hub.runs[run.summary.ID] = run
	th.hub.mu.Unlock()
	th.hub.finish(run, nil, dir)
	if closedOf("dbt_20260101_000001_aaaaaa") {
		t.Errorf("a completed debate should reopen the thread's earlier jobs")
	}
}

func TestCloseThreadRefusedWhileDebateRuns(t *testing.T) {
	th := newTestHub(t, nil)
	th.hub.mu.Lock()
	th.hub.runs["dbt_20260101_000009_dddddd"] = &debateRun{summary: debateSummary{ID: "dbt_20260101_000009_dddddd", Status: "running", ThreadID: "thread_abc1"}}
	th.hub.mu.Unlock()
	rec := th.call("POST", "/api/threads/thread_abc1/close", "")
	if rec.Code != 409 || !strings.Contains(decode(t, rec)["error"].(string), "running") {
		t.Errorf("running: %d %s", rec.Code, rec.Body.String())
	}
	if rec = th.call("POST", "/api/threads/thread_zzz/close", ""); rec.Code != 200 {
		t.Errorf("other thread: %d", rec.Code)
	}
}

func TestPublishThread(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/threads/thread_abc1/publish", "")
	if got := decode(t, rec); rec.Code != 200 || got["status"] != "PUSHED" || got["branch"] != "guardian/thread_abc1" {
		t.Fatalf("publish: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--publish-thread thread_abc1 --json --yes --actor dashboard@192.0.2.1" {
		t.Errorf("argv: %q", got)
	}
	rec = th.call("POST", "/api/threads/thread_abc1/publish", `{"branch":"feature/cap-body"}`)
	if got := decode(t, rec); rec.Code != 200 || got["branch"] != "feature/cap-body" {
		t.Fatalf("named branch: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); !strings.HasSuffix(got, " --branch=feature/cap-body") {
		t.Errorf("argv: %q", got)
	}
	// An empty object means the default branch.
	if rec = th.call("POST", "/api/threads/thread_abc1/publish", `{}`); rec.Code != 200 || strings.Contains(strings.Join(th.lastArgs(t), " "), "--branch") {
		t.Errorf("empty object: %d %v", rec.Code, th.lastArgs(t))
	}
}

func TestPublishThreadCLIOutcomes(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/threads/thread_missing/publish", "")
	if rec.Code != 404 || decode(t, rec)["error"] != "thread thread_missing not found" {
		t.Errorf("missing: %d %s", rec.Code, rec.Body.String())
	}
	rec = th.call("POST", "/api/threads/thread_refused/publish", "")
	if rec.Code != 409 || decode(t, rec)["error"] != "the project test failed" {
		t.Errorf("refused: %d %s", rec.Code, rec.Body.String())
	}
	// A push that failed is a completed publish attempt reported as ok:false, like a failed action.
	rec = th.call("POST", "/api/threads/thread_pushfail/publish", "")
	if got := decode(t, rec); rec.Code != 200 || got["ok"] != false || got["stderr"] != "remote rejected" {
		t.Errorf("push failure: %d %s", rec.Code, rec.Body.String())
	}
}

func TestPublishThreadValidatesInput(t *testing.T) {
	th := newTestHub(t, nil)
	for _, bad := range []string{"bad", "thread_", "thread_a-b"} {
		if rec := th.call("POST", "/api/threads/"+bad+"/publish", ""); rec.Code != 400 {
			t.Errorf("%s: %d", bad, rec.Code)
		}
	}
	cases := []struct {
		name, body string
		want       int
	}{
		{"not json", `branch=x`, 400},
		{"wrong type", `{"branch":5}`, 400},
		{"leading dash", `{"branch":"--force"}`, 400},
		{"space", `{"branch":"a b"}`, 400},
		{"shell", `{"branch":"a;rm -rf /"}`, 400},
		{"too long", `{"branch":"` + strings.Repeat("a", 101) + `"}`, 400},
		{"body too large", `{"branch":"a"` + strings.Repeat(" ", 2048) + `}`, 413},
	}
	for _, c := range cases {
		_ = os.Remove(filepath.Join(th.dir, "last-args.txt"))
		if rec := th.call("POST", "/api/threads/thread_abc1/publish", c.body); rec.Code != c.want {
			t.Errorf("%s: %d, want %d (%s)", c.name, rec.Code, c.want, rec.Body.String())
		}
		if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
			t.Errorf("%s: the CLI ran for rejected input", c.name)
		}
	}
}

func TestPublishPlan(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.UIActions = "off" }) // read-only: not blocked
	rec := th.call("GET", "/api/threads/thread_abc1/publish-plan", "")
	plan, _ := decode(t, rec)["plan"].(map[string]interface{})
	if rec.Code != 200 || plan["branch"] != "guardian/thread_abc1" || plan["subject"] != "fix: x" {
		t.Fatalf("plan: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--publish-thread thread_abc1 --plan --json" {
		t.Errorf("argv: %q", got)
	}
	rec = th.call("GET", "/api/threads/thread_abc1/publish-plan?branch=feature/x", "")
	if got := strings.Join(th.lastArgs(t), " "); rec.Code != 200 || !strings.HasSuffix(got, " --branch=feature/x") {
		t.Errorf("branch: %d %q", rec.Code, got)
	}
	if rec = th.call("GET", "/api/threads/thread_missing/publish-plan", ""); rec.Code != 404 {
		t.Errorf("missing: %d", rec.Code)
	}
	rec = th.call("GET", "/api/threads/thread_refused/publish-plan", "")
	if rec.Code != 409 || decode(t, rec)["error"] != "workspace is dirty" {
		t.Errorf("refused: %d %s", rec.Code, rec.Body.String())
	}
	_ = os.Remove(filepath.Join(th.dir, "last-args.txt"))
	for _, bad := range []string{"/api/threads/bad/publish-plan", "/api/threads/thread_abc1/publish-plan?branch=--force", "/api/threads/thread_abc1/publish-plan?branch=a%20b"} {
		if rec = th.call("GET", bad, ""); rec.Code != 400 {
			t.Errorf("%s: %d", bad, rec.Code)
		}
	}
	if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
		t.Errorf("the CLI ran for rejected input")
	}
}

func TestPublishThreadBlockedWhenUIActionsOff(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.UIActions = "off" })
	if rec := th.call("POST", "/api/threads/thread_abc1/publish", ""); rec.Code != 403 {
		t.Errorf("off: %d %s", rec.Code, rec.Body.String())
	}
	if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
		t.Errorf("the CLI ran although publishing is disabled")
	}
}

func TestClosedThreadsList(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/threads/closed", "")
	if got := rec.Body.String(); rec.Code != 200 || strings.TrimSpace(got) != `{"threads":[]}` {
		t.Fatalf("empty: %d %s", rec.Code, got)
	}
	if err := os.WriteFile(filepath.Join(th.dir, "closed.list"), []byte("thread_b\nthread_a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	th.hub.closed.invalidate()
	rec = th.call("GET", "/api/threads/closed", "")
	threads, _ := decode(t, rec)["threads"].([]interface{})
	if rec.Code != 200 || len(threads) != 2 || threads[0] != "thread_a" || threads[1] != "thread_b" {
		t.Fatalf("closed: %d %s", rec.Code, rec.Body.String())
	}
}

func TestThreadPublications(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/threads/thread_abc1/publications", "")
	pubs, _ := decode(t, rec)["publications"].([]interface{})
	if rec.Code != 200 || len(pubs) != 1 {
		t.Fatalf("publications: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--publications thread_abc1 --json" {
		t.Errorf("argv: %q", got)
	}
	if rec = th.call("GET", "/api/threads/thread_missing/publications", ""); rec.Code != 404 {
		t.Errorf("missing: %d", rec.Code)
	}
	if rec = th.call("GET", "/api/threads/bad/publications", ""); rec.Code != 400 {
		t.Errorf("bad thread: %d", rec.Code)
	}
}

func TestWorkspaceResetPlan(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.UIActions = "off" }) // read-only: not blocked
	rec := th.call("GET", "/api/projects/demo/workspace-reset", "")
	got := decode(t, rec)
	discard, _ := got["discard"].([]interface{})
	keep, _ := got["keep"].([]interface{})
	if rec.Code != 200 || len(discard) != 1 || len(keep) != 1 || got["workspace"] != "/w" {
		t.Fatalf("plan: %d %s", rec.Code, rec.Body.String())
	}
	if args := strings.Join(th.lastArgs(t), " "); args != "--workspace-reset demo --plan --json" {
		t.Errorf("argv: %q", args)
	}
	if rec = th.call("GET", "/api/projects/missing/workspace-reset", ""); rec.Code != 404 {
		t.Errorf("missing: %d", rec.Code)
	}
	if rec = th.call("GET", "/api/projects/Bad_Id/workspace-reset", ""); rec.Code != 400 {
		t.Errorf("bad id: %d", rec.Code)
	}
}

func TestWorkspaceResetRelease(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/projects/demo/workspace-reset", `{"confirm":true,"release":["thread_a1","thread_b2"]}`)
	if rec.Code != 200 {
		t.Fatalf("release: %d %s", rec.Code, rec.Body.String())
	}
	want := "--workspace-reset demo --yes --json --actor dashboard@192.0.2.1 --release=thread_a1 --release=thread_b2"
	if args := strings.Join(th.lastArgs(t), " "); args != want {
		t.Errorf("argv: %q", args)
	}
	_ = os.Remove(filepath.Join(th.dir, "last-args.txt"))
	tooMany := `["thread_x"` + strings.Repeat(`,"thread_x"`, maxReleaseThreads)
	for _, body := range []string{
		`{"confirm":true,"release":["abc"]}`, `{"confirm":true,"release":["thread_../x"]}`,
		`{"confirm":true,"release":["--yes"]}`, `{"confirm":true,"release":"thread_a1"}`,
		`{"confirm":true,"release":` + tooMany + `]}`,
	} {
		if rec = th.call("POST", "/api/projects/demo/workspace-reset", body); rec.Code != 400 {
			t.Errorf("body %q: %d", body, rec.Code)
		}
	}
	if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
		t.Errorf("the CLI ran for rejected input")
	}
}

func TestWorkspaceReset(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/projects/demo/workspace-reset", `{"confirm":true}`)
	if got := decode(t, rec); rec.Code != 200 || got["ok"] != true {
		t.Fatalf("reset: %d %s", rec.Code, rec.Body.String())
	}
	if args := strings.Join(th.lastArgs(t), " "); args != "--workspace-reset demo --yes --json --actor dashboard@192.0.2.1" {
		t.Errorf("argv: %q", args)
	}
	if rec = th.call("POST", "/api/projects/busy/workspace-reset", `{"confirm":true}`); rec.Code != 409 {
		t.Errorf("busy: %d", rec.Code)
	}
	if rec = th.call("POST", "/api/projects/missing/workspace-reset", `{"confirm":true}`); rec.Code != 404 {
		t.Errorf("missing: %d", rec.Code)
	}
	_ = os.Remove(filepath.Join(th.dir, "last-args.txt"))
	for _, body := range []string{``, `{}`, `{"confirm":false}`, `{"confirm":"yes"}`, `not json`} {
		if rec = th.call("POST", "/api/projects/demo/workspace-reset", body); rec.Code != 400 {
			t.Errorf("body %q: %d", body, rec.Code)
		}
	}
	if rec = th.call("POST", "/api/projects/Bad_Id/workspace-reset", `{"confirm":true}`); rec.Code != 400 {
		t.Errorf("bad id: %d", rec.Code)
	}
	if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
		t.Errorf("the CLI ran for rejected input")
	}
}

func TestWorkspaceResetBlockedWhenUIActionsOff(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.UIActions = "off" })
	if rec := th.call("POST", "/api/projects/demo/workspace-reset", `{"confirm":true}`); rec.Code != 403 {
		t.Errorf("off: %d %s", rec.Code, rec.Body.String())
	}
	if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
		t.Errorf("the CLI ran although the reset is disabled")
	}
}

func TestActionRoutesUnavailableWithoutCLI(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.CLI = filepath.Join(t.TempDir(), "missing.py") })
	for _, rt := range []struct{ method, path, body string }{
		{"GET", "/api/actions", ""},
		{"GET", "/api/actions/1", ""},
		{"POST", "/api/actions/1/run", `{"confirm_id":1}`},
		{"POST", "/api/actions/1/reject", ""},
		{"POST", "/api/actions/1/fail-stale", `{"confirm_id":1}`},
		{"GET", "/api/plans", ""},
		{"GET", "/api/plans/plan_a", ""},
		{"POST", "/api/plans/plan_a/run", `{"confirm_id":"plan_a"}`},
		{"POST", "/api/plans/plan_a/reject", ""},
		{"POST", "/api/threads/thread_a/jev-recheck", ""},
		{"POST", "/api/threads/thread_a/close", ""},
		{"POST", "/api/threads/thread_a/publish", ""},
		{"GET", "/api/threads/thread_a/publications", ""},
		{"GET", "/api/threads/closed", ""},
		{"GET", "/api/projects/demo/workspace-reset", ""},
		{"POST", "/api/projects/demo/workspace-reset", `{"confirm":true}`},
	} {
		if rec := th.call(rt.method, rt.path, rt.body); rec.Code != 503 {
			t.Errorf("%s %s: %d, want 503", rt.method, rt.path, rec.Code)
		}
	}
}

func TestActorFor(t *testing.T) {
	valid := regexp.MustCompile(`^[A-Za-z0-9@:._-]{1,64}$`)
	cases := []struct{ in, want string }{
		{"192.0.2.1:5555", "dashboard@192.0.2.1"},
		{"[2001:db8::1]:443", "dashboard@2001:db8::1"},
		{"[fe80::1%en0]:443", "dashboard@fe80::1_en0"},
		{"no-port", "dashboard"},
		{"", "dashboard"},
		{"ev il$(id);`x`/..:80", "dashboard@ev_il__id___x__.."},
	}
	for _, c := range cases {
		got := actorFor(c.in)
		if got != c.want {
			t.Errorf("actorFor(%q) = %q, want %q", c.in, got, c.want)
		}
		if !valid.MatchString(got) {
			t.Errorf("actorFor(%q) = %q is not a valid actor", c.in, got)
		}
	}
	long := actorFor(strings.Repeat("a", 200) + ":1")
	if len(long) != 64 || !valid.MatchString(long) {
		t.Errorf("long actor: %q (%d)", long, len(long))
	}
}

func TestProjectRoutes(t *testing.T) {
	th := newTestHub(t, nil)
	if rec := th.call("GET", "/api/projects", ""); rec.Code != 200 || decode(t, rec)["projects"] == nil {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	if rec := th.call("POST", "/api/projects", `{"id":"svc"}`); rec.Code != 200 {
		t.Fatalf("save: %d %s", rec.Code, rec.Body.String())
	}
	if rec := th.call("POST", "/api/projects/svc/sync", ""); rec.Code != 200 {
		t.Fatalf("sync: %d %s", rec.Code, rec.Body.String())
	}
	args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if got := strings.Fields(string(args)); strings.Join(got, " ") != "--project-sync svc --json" {
		t.Errorf("sync argv %q", got)
	}
	if rec := th.call("POST", "/api/projects/missing/sync", ""); rec.Code != 400 {
		t.Errorf("sync of unknown project: %d", rec.Code)
	}
	if rec := th.call("POST", "/api/projects/..%2Fx/sync", ""); rec.Code != 400 && rec.Code != 404 {
		t.Errorf("bad id: %d", rec.Code)
	}
	if rec := th.call("DELETE", "/api/projects/svc", ""); rec.Code != 200 {
		t.Errorf("delete: %d", rec.Code)
	}
}

func TestDebateProjectArgvAndFilter(t *testing.T) {
	th := newTestHub(t, nil)
	if rec := th.call("POST", "/api/debates", `{"task":"x","project":"Bad Project"}`); rec.Code != 400 {
		t.Fatalf("invalid project accepted: %d", rec.Code)
	}
	rec := th.call("POST", "/api/debates", `{"task":"review","project":"svc"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	id := decode(t, rec)["id"].(string)
	th.waitDone(t, id)
	data, _ := os.ReadFile(filepath.Join(th.dir, "debate-args.txt"))
	if !strings.Contains(string(data), "--project=svc\n") {
		t.Errorf("argv lacks --project=svc: %q", data)
	}
	rec = th.call("POST", "/api/debates", `{"task":"default one"}`)
	th.waitDone(t, decode(t, rec)["id"].(string))

	count := func(query string) int {
		rec := th.call("GET", "/api/debates"+query, "")
		if rec.Code != 200 {
			t.Fatalf("list %s: %d", query, rec.Code)
		}
		return len(decode(t, rec)["debates"].([]interface{}))
	}
	if all, svc, def := count(""), count("?project=svc"), count("?project=server-guardian"); all != 2 || svc != 1 || def != 1 {
		t.Errorf("all %d svc %d default %d", all, svc, def)
	}
	if rec := th.call("GET", "/api/debates?project=BAD!", ""); rec.Code != 400 {
		t.Errorf("bad project filter: %d", rec.Code)
	}
	if rec := th.call("GET", "/api/actions?project=svc", ""); rec.Code != 200 {
		t.Fatalf("actions filter: %d", rec.Code)
	}
	args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	if !strings.Contains(string(args), "--project=svc") {
		t.Errorf("actions argv lacks project: %q", args)
	}
}

func TestGroupRoutes(t *testing.T) {
	servers := filepath.Join(t.TempDir(), "servers.json")
	if err := os.WriteFile(servers, []byte(`[{"id":"a"}]`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GUARDIAN_SERVERS_FILE", servers)
	th := newTestHub(t, nil)
	argv := func() string {
		args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
		return strings.Join(strings.Fields(string(args)), " ")
	}

	rec := th.call("GET", "/api/groups", "")
	if rec.Code != 200 || decode(t, rec)["groups"] == nil {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	// The CLI resolves membership against the same servers.json as the agent.
	if got := decode(t, rec)["servers_file"]; got != servers {
		t.Errorf("GUARDIAN_SERVERS_FILE passed to the CLI = %v, want %s", got, servers)
	}
	if got := argv(); got != "--groups --json" {
		t.Errorf("list argv %q", got)
	}
	if rec = th.call("POST", "/api/groups", `{"name":"ARM"}`); rec.Code != 200 || argv() != "--group-save --json" {
		t.Errorf("save: %d %s argv %q", rec.Code, rec.Body.String(), argv())
	}
	if rec = th.call("POST", "/api/groups", `{"name":"BAD"}`); rec.Code != 400 || decode(t, rec)["error"] != "bad" {
		t.Errorf("invalid save: %d %s", rec.Code, rec.Body.String())
	}
	if rec = th.call("POST", "/api/groups/preview", `{"include":["a"]}`); rec.Code != 200 || argv() != "--group-preview --json" {
		t.Errorf("preview: %d argv %q", rec.Code, argv())
	}
	if rec = th.call("POST", "/api/groups", strings.Repeat("a", configBodyLimit+1)); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body: %d", rec.Code)
	}
	if rec = th.call("DELETE", "/api/groups/arm", ""); rec.Code != 200 || argv() != "--group-delete arm --json" {
		t.Errorf("delete: %d argv %q", rec.Code, argv())
	}
	if rec = th.call("DELETE", "/api/groups/missing", ""); rec.Code != 404 {
		t.Errorf("delete of unknown group: %d %s", rec.Code, rec.Body.String())
	}
	for _, id := range []string{"UPPER", "-x", "a;b", strings.Repeat("a", 65)} {
		if rec = th.call("DELETE", "/api/groups/"+id, ""); rec.Code != 400 {
			t.Errorf("delete %q: %d, want 400", id, rec.Code)
		}
	}
}

func TestListDebatesIncludesRetrieval(t *testing.T) {
	th := newTestHub(t, nil)
	cases := map[string]string{
		"dbt_20260101_000001_aaaaaa": `{"thread_id":"t","retrieval":{"results":7,"bytes":1234,"by_agent":{"a":{"results":7,"bytes":1234,"steps":3}}}}`,
		"dbt_20260101_000002_bbbbbb": `{"thread_id":"t"`,                          // malformed
		"dbt_20260101_000003_cccccc": ``,                                          // no result.json
		"dbt_20260101_000004_dddddd": `{"thread_id":"t","retrieval":"nope"}`,      // wrong shape
		"dbt_20260101_000005_eeeeee": `{"thread_id":"t","retrieval":{"bytes":5}}`, // missing field
	}
	for id, body := range cases {
		th.writeJobMeta(t, id, "thread_x", false)
		if body != "" {
			if err := os.WriteFile(filepath.Join(th.hub.jobsDir(), id, "result.json"), []byte(body), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	}
	rec := th.call("GET", "/api/debates", "")
	if rec.Code != 200 {
		t.Fatalf("list: %d", rec.Code)
	}
	var out struct {
		Debates []struct {
			ID        string                 `json:"id"`
			Retrieval map[string]interface{} `json:"retrieval"`
		} `json:"debates"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if len(out.Debates) != len(cases) {
		t.Fatalf("got %d entries", len(out.Debates))
	}
	for _, d := range out.Debates {
		if d.ID == "dbt_20260101_000001_aaaaaa" {
			if d.Retrieval["results"] != float64(7) || d.Retrieval["bytes"] != float64(1234) || len(d.Retrieval) != 2 {
				t.Errorf("retrieval = %v", d.Retrieval)
			}
		} else if d.Retrieval != nil {
			t.Errorf("%s: unexpected retrieval %v", d.ID, d.Retrieval)
		}
	}
	rec = th.call("GET", "/api/debates/dbt_20260101_000001_aaaaaa", "")
	if !strings.Contains(rec.Body.String(), `"retrieval":{"results":7,"bytes":1234}`) {
		t.Errorf("detail lacks retrieval: %s", rec.Body.String())
	}
}

// A cancel that lands after the job is registered but before its process exists must not
// panic, and launch must then kill the process it starts instead of letting it run.
func TestDebateCancelBeforeProcessStarts(t *testing.T) {
	th := newTestHub(t, nil)
	run := &debateRun{summary: debateSummary{ID: newJobID(), Task: "slow one", Status: "running", StartedAt: time.Now().UTC()}}
	th.hub.mu.Lock()
	th.hub.runs[run.summary.ID] = run
	th.hub.mu.Unlock()

	if rec := th.call("DELETE", "/api/debates/"+run.summary.ID, ""); rec.Code != http.StatusOK {
		t.Fatalf("cancel before start: %d %s", rec.Code, rec.Body.String())
	}
	if err := th.hub.launch(run, []string{"--debate=slow one"}); err != nil {
		t.Fatal(err)
	}
	m := th.waitDone(t, run.summary.ID)
	waitUntil(t, "exit code recorded", func() bool {
		m = decode(t, th.call("GET", "/api/debates/"+run.summary.ID, ""))
		return m["exit_code"] != nil
	})
	if m["status"] != "cancelled" {
		t.Errorf("status = %v, want cancelled", m["status"])
	}
	if data, err := os.ReadFile(filepath.Join(th.dir, "child.pid")); err == nil {
		if pid, err := strconv.Atoi(strings.TrimSpace(string(data))); err == nil {
			waitUntil(t, "grandchild killed", func() bool { return syscall.Kill(pid, 0) != nil })
		}
	}
}

// Threads closed outside the dashboard (CLI --close-thread) are hidden too: the list follows the DB,
// asks it at most once per TTL, and asks again after a close or a reopen.
func TestListFollowsClosedThreadsInDB(t *testing.T) {
	th := newTestHub(t, nil)
	th.writeJobMeta(t, "dbt_20260101_000001_aaaaaa", "thread_cli1", false)
	th.writeJobMeta(t, "dbt_20260101_000002_bbbbbb", "thread_stale", true)
	if err := os.WriteFile(filepath.Join(th.dir, "closed.list"), []byte("thread_cli1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	closedIn := func() map[string]bool {
		got := map[string]bool{}
		for _, d := range decode(t, th.call("GET", "/api/debates", ""))["debates"].([]interface{}) {
			m := d.(map[string]interface{})
			got[m["thread_id"].(string)] = m["thread_closed"] == true
		}
		return got
	}
	calls := func() int {
		data, _ := os.ReadFile(filepath.Join(th.dir, "closed-calls"))
		return strings.Count(string(data), "x")
	}
	if got := closedIn(); !got["thread_cli1"] || got["thread_stale"] {
		t.Fatalf("list should follow the DB: %v", got)
	}
	closedIn()
	if n := calls(); n != 1 {
		t.Errorf("CLI asked %d times for two list calls, want 1 (cached)", n)
	}
	if rec := th.call("POST", "/api/threads/thread_stale/close", ""); rec.Code != 200 {
		t.Fatalf("close: %d", rec.Code)
	}
	if got := closedIn(); !got["thread_stale"] {
		t.Errorf("a dashboard close must show at once: %v", got)
	}
	if n := calls(); n != 2 {
		t.Errorf("close should invalidate the cache: %d CLI calls", n)
	}
}

// Without the CLI the list keeps the thread_closed flag stored in each job.
func TestListKeepsStoredFlagWithoutCLI(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.CLI = filepath.Join(t.TempDir(), "missing.py") })
	th.writeJobMeta(t, "dbt_20260101_000001_aaaaaa", "thread_x", true)
	rec := th.call("GET", "/api/debates", "")
	if !strings.Contains(rec.Body.String(), `"thread_closed":true`) {
		t.Errorf("stored flag lost: %s", rec.Body.String())
	}
}

// A thread with a pushed publication is final: the list marks its jobs thread_published, and continuing it
// is refused with 409 before any job is created. A publish refreshes the cached set.
func TestPublishedThreadsOverlayAndRefusal(t *testing.T) {
	th := newTestHub(t, nil)
	th.writeJobMeta(t, "dbt_20260101_000001_aaaaaa", "thread_pub1", false)
	th.writeJobMeta(t, "dbt_20260101_000002_bbbbbb", "thread_free", false)
	if err := os.WriteFile(filepath.Join(th.dir, "published.list"), []byte("thread_pub1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	publishedIn := func() map[string]bool {
		got := map[string]bool{}
		for _, d := range decode(t, th.call("GET", "/api/debates", ""))["debates"].([]interface{}) {
			m := d.(map[string]interface{})
			got[m["thread_id"].(string)] = m["thread_published"] == true
		}
		return got
	}
	if got := publishedIn(); !got["thread_pub1"] || got["thread_free"] {
		t.Fatalf("list should mark only the published thread: %v", got)
	}

	rec := th.call("POST", "/api/debates", `{"task":"more","thread_id":"thread_pub1"}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("continue a published thread: %d %s", rec.Code, rec.Body.String())
	}
	if got := decode(t, rec)["error"]; got != "thread thread_pub1 was published as branch guardian/thread_pub1; start a new thread" {
		t.Errorf("error text: %v", got)
	}
	th.hub.mu.Lock()
	running := len(th.hub.runs)
	th.hub.mu.Unlock()
	if running != 0 {
		t.Errorf("a refused continue must not leave a job: %d", running)
	}
	if rec := th.call("POST", "/api/debates", `{"task":"again","thread_id":"thread_free"}`); rec.Code != http.StatusAccepted {
		t.Errorf("an unpublished thread still continues: %d %s", rec.Code, rec.Body.String())
	}

	// A publish invalidates the cache, so the new state shows at once.
	if err := os.WriteFile(filepath.Join(th.dir, "published.list"), []byte("thread_pub1\nthread_free\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := publishedIn(); got["thread_free"] {
		t.Fatalf("cache should still hold the old set: %v", got)
	}
	if rec := th.call("POST", "/api/threads/thread_free/publish", ""); rec.Code != 200 {
		t.Fatalf("publish: %d %s", rec.Code, rec.Body.String())
	}
	if got := publishedIn(); !got["thread_free"] {
		t.Errorf("publish should refresh the published set: %v", got)
	}
}

func TestFailStaleActionRoute(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/actions/12/fail-stale", `{"confirm_id":12}`)
	if rec.Code != 200 || decode(t, rec)["ok"] != true {
		t.Fatalf("fail-stale: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--fail-stale-action 12 --json --actor dashboard@192.0.2.1" {
		t.Errorf("argv: %q", got)
	}
	if rec := th.call("POST", "/api/actions/3/fail-stale", `{"confirm_id":3}`); rec.Code != 404 {
		t.Errorf("unknown: %d %s", rec.Code, rec.Body.String())
	}
	if rec := th.call("POST", "/api/actions/4/fail-stale", `{"confirm_id":4}`); rec.Code != 409 {
		t.Errorf("not running: %d %s", rec.Code, rec.Body.String())
	}
	if rec := th.call("POST", "/api/actions/x/fail-stale", `{"confirm_id":1}`); rec.Code != 400 {
		t.Errorf("bad id: %d", rec.Code)
	}
}

func TestFailStaleActionConfirmAndLive(t *testing.T) {
	th := newTestHub(t, nil)
	for _, body := range []string{`{"confirm_id":13}`, `{}`, `{"confirm_id":"12"}`, `not json`, ``} {
		if rec := th.call("POST", "/api/actions/12/fail-stale", body); rec.Code != 400 {
			t.Errorf("body %q: %d, want 400", body, rec.Code)
		}
	}
	if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
		t.Error("the CLI ran despite a bad confirmation")
	}
	th.hub.mu.Lock()
	th.hub.actionRuns[12] = struct{}{}
	th.hub.mu.Unlock()
	if rec := th.call("POST", "/api/actions/12/fail-stale", `{"confirm_id":12}`); rec.Code != 409 {
		t.Errorf("live run: %d %s", rec.Code, rec.Body.String())
	}
	if _, err := os.Stat(filepath.Join(th.dir, "last-args.txt")); err == nil {
		t.Error("the CLI ran while this hub is running the action")
	}
}

// Every decision route answers 403 naming the CLI command when GUARDIAN_UI_ACTIONS=off, and works when on.
func TestUIActionsOffBlocksEveryDecisionRoute(t *testing.T) {
	routes := []struct{ path, body, cli string }{
		{"/api/actions/5/run", `{"confirm_id":5}`, "--run-action 5"},
		{"/api/actions/5/reject", "", "--reject-action 5"},
		{"/api/actions/5/fail-stale", `{"confirm_id":5}`, "--fail-stale-action 5"},
		{"/api/plans/plan_a_r1/run", `{"confirm_id":"plan_a_r1"}`, "--plan-run plan_a_r1"},
		{"/api/plans/plan_a_r1/reject", "", "--plan-reject plan_a_r1"},
		{"/api/threads/thread_a/jev-recheck", "", "--jev-recheck thread_a"},
		{"/api/threads/thread_a/close", "", "--close-thread thread_a"},
		{"/api/threads/thread_a/publish", "", "--publish-thread thread_a"},
		{"/api/projects/demo/workspace-reset", `{"confirm":true}`, "--workspace-reset demo"},
	}
	off := newTestHub(t, func(c *debateConfig) { c.UIActions = "off" })
	on := newTestHub(t, nil)
	for _, rt := range routes {
		rec := off.call("POST", rt.path, rt.body)
		msg, _ := decode(t, rec)["error"].(string)
		if rec.Code != 403 || !strings.Contains(msg, "GUARDIAN_UI_ACTIONS=off") || !strings.Contains(msg, "guardian.py "+rt.cli) {
			t.Errorf("off %s: %d %s", rt.path, rec.Code, rec.Body.String())
		}
		if rec := on.call("POST", rt.path, rt.body); rec.Code != 200 {
			t.Errorf("on %s: %d %s", rt.path, rec.Code, rec.Body.String())
		}
	}
	if _, err := os.Stat(filepath.Join(off.dir, "last-args.txt")); err == nil {
		t.Error("the CLI ran while decisions are disabled")
	}
	for _, p := range []string{"/api/actions", "/api/actions/5", "/api/plans", "/api/plans/plan_a_r1", "/api/threads/closed", "/api/threads/thread_a/publish-plan",
		"/api/projects/demo/workspace-reset", "/api/threads/thread_a/publications"} {
		if rec := off.call("GET", p, ""); rec.Code != 200 {
			t.Errorf("off read-only %s: %d %s", p, rec.Code, rec.Body.String())
		}
	}
}

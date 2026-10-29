package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// fakeWikiCLI mimics the guardian.py wiki commands. Projects named busy/bad/ghostly select the
// early exits 4/2/3 of --code-wiki-build; a project starting with slow keeps the build running.
const fakeWikiCLI = `#!/bin/sh
dir=$(dirname "$0")
printf '%s\n' "$@" > "$dir/last-args.txt"
mode=""; project=""; rf=""; ef=""; id=""; pages=""; actor=""
while [ $# -gt 0 ]; do
  case "$1" in
    --code-wiki-list) mode=list ;;
    --code-wiki-show=*) mode=show; id="${1#--code-wiki-show=}" ;;
    --code-wiki-save) mode=save ;;
    --code-wiki-delete=*) mode=delete; id="${1#--code-wiki-delete=}" ;;
    --code-wiki-build) mode=build ;;
    --debate=*) mode=debate ;;
    --project=*) project="${1#--project=}" ;;
    --actor=*) actor="${1#--actor=}" ;;
    --page=*) pages="$pages ${1#--page=}" ;;
    --result-file) shift; rf="$1" ;;
    --events-file=*) ef="${1#--events-file=}" ;;
  esac
  shift
done
case "$mode" in
  list)
    if [ "$project" = "nope" ]; then echo '{"ok":false,"error":"no project nope"}'; exit 2; fi
    printf '{"ok":true,"project":"%s","sha":"abc","pages":[{"id":"cli-llm-agent","title":"T","scope":["cli/llm-agent/"],"author":{"kind":"human","user":"u"},"updated":"2026-10-12T09:00:00Z","state":"stale"}],"errors":[]}\n' "$project" ;;
  show)
    case "$id" in
      gone) echo '{"ok":false,"error":"page not found"}'; exit 3 ;;
      crash) echo boom >&2; exit 1 ;;
    esac
    printf '{"ok":true,"project":"%s","page":{"id":"%s"},"narrative":"n","state":"fresh","staleness":{},"generated":"g"}\n' "$project" "$id" ;;
  save)
    cat > "$dir/last-stdin.txt"
    if grep -q BAD "$dir/last-stdin.txt"; then echo '{"ok":false,"error":"bad page"}'; exit 2; fi
    printf '{"ok":true,"project":"%s","page":{"id":"x"},"state":"fresh","actor":"%s"}\n' "$project" "$actor" ;;
  delete)
    if [ "$id" = "gone" ]; then echo '{"ok":false,"error":"page not found"}'; exit 3; fi
    printf '{"ok":true,"project":"%s","deleted":"%s"}\n' "$project" "$id" ;;
  build)
    cp "$dir/last-args.txt" "$dir/build-args.txt"
    case "$project" in
      busy*) echo '{"ok":false,"error":"another code wiki build is running for this project"}'; exit 4 ;;
      bad*) echo '{"ok":false,"error":"pages exist already: pass --page ID, --stale or --all"}'; exit 2 ;;
    esac
    case "$pages" in
      *ghost*) echo '{"ok":false,"error":"unknown page ghost"}'; exit 3 ;;
    esac
    echo '{"seq":1,"ts":"2026-10-12T00:00:00.000Z","thread_id":"","type":"wiki_build_started","data":{"project":"'"$project"'","pages":["cli-llm-agent"]}}' > "$ef"
    case "$project" in
      slow*) sleep 30 & echo $! > "$dir/child.pid"; wait ;;
    esac
    printf '{"project":"%s","pages":[],"written":[],"skipped":[],"failed":0}\n' "$project" > "$rf"
    exit 0 ;;
  debate)
    sleep 30 & echo $! > "$dir/child.pid"; wait ;;
esac
`

func newWikiHub(t *testing.T, mutate func(*debateConfig)) *testHub {
	t.Helper()
	dir := t.TempDir()
	cli := filepath.Join(dir, "fake-guardian.sh")
	if err := os.WriteFile(cli, []byte(fakeWikiCLI), 0o755); err != nil {
		t.Fatal(err)
	}
	cfg := debateConfig{
		CLI: cli, Python: "sh", DataDir: filepath.Join(dir, "data"),
		MaxRunning: 2, JobTimeout: time.Minute, ConfigLimit: 10 * time.Second,
		MaxWikiBuilds: 1, WikiStartGrace: 400 * time.Millisecond,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	hub := newDebateHub(cfg)
	mux := http.NewServeMux()
	hub.register(mux)
	th := &testHub{hub: hub, h: mux, dir: dir, cli: cli}
	// Registered after t.TempDir, so it runs first: stop every job and wait until their goroutines have
	// written the final meta.json, or the directory removal races with them.
	t.Cleanup(func() {
		hub.mu.Lock()
		ids := make([]string, 0, len(hub.runs))
		for id := range hub.runs {
			ids = append(ids, id)
		}
		hub.mu.Unlock()
		for _, id := range ids {
			th.call("DELETE", "/api/debates/"+id, "")
		}
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			hub.mu.Lock()
			n := len(hub.runs)
			hub.mu.Unlock()
			if n == 0 {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	})
	return th
}

func TestWikiReadRoutesArgv(t *testing.T) {
	th := newWikiHub(t, nil)
	rec := th.call("GET", "/api/projects/server-guardian/wiki?sha=abc123", "")
	if rec.Code != 200 {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["project"] != "server-guardian" || m["pages"] == nil {
		t.Errorf("list body: %v", m)
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--code-wiki-list --project=server-guardian --json --sha=abc123" {
		t.Errorf("list argv: %q", got)
	}

	rec = th.call("GET", "/api/projects/server-guardian/wiki/cli-llm-agent", "")
	if rec.Code != 200 || decode(t, rec)["narrative"] != "n" {
		t.Fatalf("show: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--code-wiki-show=cli-llm-agent --project=server-guardian --json" {
		t.Errorf("show argv: %q", got)
	}

	rec = th.call("DELETE", "/api/projects/server-guardian/wiki/cli-llm-agent", "")
	if rec.Code != 200 || decode(t, rec)["deleted"] != "cli-llm-agent" {
		t.Fatalf("delete: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--code-wiki-delete=cli-llm-agent --project=server-guardian --json" {
		t.Errorf("delete argv: %q", got)
	}
}

func TestWikiValidation(t *testing.T) {
	th := newWikiHub(t, nil)
	cases := []struct{ method, path, body, want string }{
		{"GET", "/api/projects/Bad_ID/wiki", "", "invalid id"},
		{"GET", "/api/projects/p/wiki?sha=-x", "", "invalid sha"},
		{"GET", "/api/projects/p/wiki?sha=a%20b", "", "invalid sha"},
		{"GET", "/api/projects/p/wiki/Upper", "", "invalid page id"},
		{"GET", "/api/projects/p/wiki/-lead", "", "invalid page id"},
		{"GET", "/api/projects/p/wiki/under_score", "", "invalid page id"},
		{"GET", "/api/projects/p/wiki/" + strings.Repeat("a", 65), "", "invalid page id"},
		{"DELETE", "/api/projects/p/wiki/A", "", "invalid page id"},
		{"PUT", "/api/projects/p/wiki/A", `{"title":"t","scope":[],"body":"b"}`, "invalid page id"},
		{"PUT", "/api/projects/p/wiki/ok", `[1]`, "Invalid JSON body"},
		{"PUT", "/api/projects/p/wiki/ok", `nope`, "Invalid JSON body"},
		{"PUT", "/api/projects/p/wiki/ok", `{"id":"other","body":"b"}`, "body id must equal"},
		{"PUT", "/api/projects/p/wiki/ok", `{"id":5,"body":"b"}`, "body id must equal"},
		{"POST", "/api/projects/p/wiki/build", `{"pages":["Bad"]}`, "invalid page id"},
		{"POST", "/api/projects/p/wiki/build", `{"pages":["a"],"stale":true}`, "choose one"},
		{"POST", "/api/projects/p/wiki/build", `{"stale":true,"all":true}`, "choose one"},
		{"POST", "/api/projects/p/wiki/build", `{"bogus":1}`, "Invalid JSON body"},
		{"POST", "/api/projects/p/wiki/build", `{"kind":"folder"}`, "kind must be dir, overview, group or feature"},
		{"POST", "/api/projects/p/wiki/build", `{"kind":"feature","pages":["a"]}`, "kind selects pages itself"},
		{"POST", "/api/projects/p/wiki/build", `not json`, "Invalid JSON body"},
		{"POST", "/api/projects/BAD/wiki/build", `{}`, "invalid id"},
	}
	for _, c := range cases {
		rec := th.call(c.method, c.path, c.body)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s %s: status %d, want 400 (%s)", c.method, c.path, rec.Code, rec.Body.String())
			continue
		}
		if got, _ := decode(t, rec)["error"].(string); !strings.Contains(got, c.want) {
			t.Errorf("%s %s: error %q, want %q", c.method, c.path, got, c.want)
		}
	}
	tooMany := make([]string, maxWikiBuildPages+1)
	for i := range tooMany {
		tooMany[i] = "p" + strconv.Itoa(i)
	}
	body, _ := json.Marshal(map[string]interface{}{"pages": tooMany})
	if rec := th.call("POST", "/api/projects/p/wiki/build", string(body)); rec.Code != http.StatusBadRequest {
		t.Errorf("too many pages: %d", rec.Code)
	}
}

func TestWikiExitCodeMapping(t *testing.T) {
	th := newWikiHub(t, nil)
	cases := []struct {
		method, path, body string
		want               int
	}{
		{"GET", "/api/projects/nope/wiki", "", http.StatusBadRequest},
		{"GET", "/api/projects/p/wiki/gone", "", http.StatusNotFound},
		{"GET", "/api/projects/p/wiki/crash", "", http.StatusInternalServerError},
		{"DELETE", "/api/projects/p/wiki/gone", "", http.StatusNotFound},
		{"PUT", "/api/projects/p/wiki/ok", `{"title":"BAD","scope":[],"body":"b"}`, http.StatusBadRequest},
	}
	for _, c := range cases {
		rec := th.call(c.method, c.path, c.body)
		if rec.Code != c.want {
			t.Errorf("%s %s: %d, want %d (%s)", c.method, c.path, rec.Code, c.want, rec.Body.String())
		}
	}
	if rec := th.call("GET", "/api/projects/p/wiki/gone", ""); decode(t, rec)["error"] != "page not found" {
		t.Errorf("CLI error text is not passed through: %s", rec.Body.String())
	}
}

func TestWikiSaveFillsIDAndActor(t *testing.T) {
	th := newWikiHub(t, nil)
	rec := th.call("PUT", "/api/projects/server-guardian/wiki/my-page", `{"title":"T","scope":["a/"],"body":"hello"}`)
	if rec.Code != 200 {
		t.Fatalf("save: %d %s", rec.Code, rec.Body.String())
	}
	if decode(t, rec)["actor"] != "dashboard@192.0.2.1" {
		t.Errorf("actor: %s", rec.Body.String())
	}
	stdin, _ := os.ReadFile(filepath.Join(th.dir, "last-stdin.txt"))
	var sent map[string]interface{}
	if err := json.Unmarshal(stdin, &sent); err != nil {
		t.Fatalf("stdin is not JSON: %q", stdin)
	}
	if sent["id"] != "my-page" || sent["title"] != "T" || sent["body"] != "hello" {
		t.Errorf("stdin: %v", sent)
	}
	args := strings.Join(th.lastArgs(t), " ")
	if args != "--code-wiki-save --project=server-guardian --actor=dashboard@192.0.2.1 --json" {
		t.Errorf("save argv: %q", args)
	}
	// An id in the body equal to the path is fine.
	if rec = th.call("PUT", "/api/projects/server-guardian/wiki/my-page", `{"id":"my-page","title":"T","scope":[],"body":"b"}`); rec.Code != 200 {
		t.Errorf("matching body id: %d %s", rec.Code, rec.Body.String())
	}
}

func TestWikiSaveSizeLimit(t *testing.T) {
	th := newWikiHub(t, nil)
	save := func(text string) *saveResult {
		body, _ := json.Marshal(map[string]interface{}{"title": "T", "scope": []string{}, "body": text})
		rec := th.call("PUT", "/api/projects/p/wiki/big", string(body))
		return &saveResult{rec.Code, rec.Body.String()}
	}
	if r := save(strings.Repeat("a", wikiTextLimit)); r.code != 200 {
		t.Errorf("text at the limit: %d %s", r.code, r.body)
	}
	if r := save(strings.Repeat("a", wikiTextLimit+1)); r.code != http.StatusRequestEntityTooLarge {
		t.Errorf("text over the limit: %d %s", r.code, r.body)
	}
	if r := save(strings.Repeat("a", wikiBodyLimit+10)); r.code != http.StatusRequestEntityTooLarge {
		t.Errorf("request over the limit: %d", r.code)
	}
	// JSON overhead: a text of escaped characters below the text limit still fits the request limit.
	if r := save(strings.Repeat("\n", wikiTextLimit/2)); r.code != 200 {
		t.Errorf("escaped text: %d %s", r.code, r.body)
	}
}

type saveResult struct {
	code int
	body string
}

func TestWikiBuildArgvAndJob(t *testing.T) {
	th := newWikiHub(t, nil)
	cases := []struct {
		body string
		want []string
	}{
		{`{"pages":["a","b"]}`, []string{"--code-wiki-build", "--project=proj", "--json", "--page=a", "--page=b"}},
		{`{"stale":true}`, []string{"--code-wiki-build", "--project=proj", "--json", "--stale"}},
		{`{"all":true,"force":true}`, []string{"--code-wiki-build", "--project=proj", "--json", "--all", "--force"}},
		{``, []string{"--code-wiki-build", "--project=proj", "--json"}},
		{`{}`, []string{"--code-wiki-build", "--project=proj", "--json"}},
		{`{"kind":"feature"}`, []string{"--code-wiki-build", "--project=proj", "--json", "--kind=feature"}},
		{`{"kind":"group"}`, []string{"--code-wiki-build", "--project=proj", "--json", "--kind=group"}},
		{`{"stale":true,"kind":"dir"}`, []string{"--code-wiki-build", "--project=proj", "--json", "--stale", "--kind=dir"}},
	}
	for _, c := range cases {
		rec := th.call("POST", "/api/projects/proj/wiki/build", c.body)
		if rec.Code != http.StatusAccepted {
			t.Fatalf("%q: %d %s", c.body, rec.Code, rec.Body.String())
		}
		m := decode(t, rec)
		id := m["id"].(string)
		if m["kind"] != "code_wiki" || m["project"] != "proj" || !jobIDPattern.MatchString(id) {
			t.Errorf("summary: %v", m)
		}
		done := th.waitDone(t, id)
		if done["status"] != "completed" || done["kind"] != "code_wiki" {
			t.Errorf("job %v", done)
		}
		args, _ := os.ReadFile(filepath.Join(th.dir, "build-args.txt"))
		got := strings.Split(strings.TrimSpace(string(args)), "\n")
		// launch prepends --result-file <path> and --events-file=<path>.
		if len(got) < 3 || got[0] != "--result-file" || !strings.HasSuffix(got[1], filepath.Join(id, "result.json")) ||
			got[2] != "--events-file="+filepath.Join(th.hub.jobsDir(), id, "events.jsonl") {
			t.Fatalf("job file args: %v", got)
		}
		if strings.Join(got[3:], " ") != strings.Join(c.want, " ") {
			t.Errorf("%q argv: %v, want %v", c.body, got[3:], c.want)
		}
	}
}

func TestWikiBuildEarlyRejections(t *testing.T) {
	th := newWikiHub(t, nil)
	cases := []struct {
		project, body string
		want          int
		msg           string
	}{
		{"busy-one", `{"stale":true}`, http.StatusConflict, "another code wiki build is running"},
		{"bad-one", `{}`, http.StatusBadRequest, "pages exist already"},
		{"proj", `{"pages":["ghost"]}`, http.StatusNotFound, "unknown page ghost"},
	}
	for _, c := range cases {
		rec := th.call("POST", "/api/projects/"+c.project+"/wiki/build", c.body)
		if rec.Code != c.want {
			t.Errorf("%s: %d, want %d (%s)", c.project, rec.Code, c.want, rec.Body.String())
			continue
		}
		if got, _ := decode(t, rec)["error"].(string); !strings.Contains(got, c.msg) {
			t.Errorf("%s: error %q", c.project, got)
		}
	}
	// Rejected builds leave no job behind and free the slot.
	entries, _ := os.ReadDir(th.hub.jobsDir())
	if len(entries) != 0 {
		t.Errorf("rejected builds left %d job directories", len(entries))
	}
	th.hub.mu.Lock()
	n := len(th.hub.runs)
	th.hub.mu.Unlock()
	if n != 0 {
		t.Errorf("%d runs still registered", n)
	}
}

func TestWikiBuildSlotAndSameProject409(t *testing.T) {
	th := newWikiHub(t, nil)
	first := th.call("POST", "/api/projects/slow-a/wiki/build", `{"stale":true}`)
	if first.Code != http.StatusAccepted {
		t.Fatalf("first: %d %s", first.Code, first.Body.String())
	}
	id := decode(t, first)["id"].(string)
	if m := decode(t, th.call("GET", "/api/debates/"+id, "")); m["status"] != "running" {
		t.Fatalf("job should be running: %v", m)
	}
	// Same project: 409. Another project: the single slot is taken, 429.
	if rec := th.call("POST", "/api/projects/slow-a/wiki/build", `{"stale":true}`); rec.Code != http.StatusConflict {
		t.Errorf("same project: %d %s", rec.Code, rec.Body.String())
	}
	if rec := th.call("POST", "/api/projects/slow-b/wiki/build", `{"stale":true}`); rec.Code != http.StatusTooManyRequests {
		t.Errorf("second project: %d %s", rec.Code, rec.Body.String())
	}

	// Cancel through the debate route: kills the process group and frees the slot.
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
	waitUntil(t, "slot released", func() bool {
		th.hub.mu.Lock()
		defer th.hub.mu.Unlock()
		return len(th.hub.runs) == 0
	})
	if rec = th.call("POST", "/api/projects/slow-b/wiki/build", `{"stale":true}`); rec.Code != http.StatusAccepted {
		t.Errorf("after cancel: %d %s", rec.Code, rec.Body.String())
	} else {
		th.call("DELETE", "/api/debates/"+decode(t, rec)["id"].(string), "")
	}
}

func TestWikiBuildSlotsAreConfigurable(t *testing.T) {
	th := newWikiHub(t, func(c *debateConfig) { c.MaxWikiBuilds = 2 })
	var ids []string
	for _, p := range []string{"slow-a", "slow-b"} {
		rec := th.call("POST", "/api/projects/"+p+"/wiki/build", `{"stale":true}`)
		if rec.Code != http.StatusAccepted {
			t.Fatalf("%s: %d %s", p, rec.Code, rec.Body.String())
		}
		ids = append(ids, decode(t, rec)["id"].(string))
	}
	if rec := th.call("POST", "/api/projects/slow-c/wiki/build", `{"stale":true}`); rec.Code != http.StatusTooManyRequests {
		t.Errorf("third: %d", rec.Code)
	}
	for _, id := range ids {
		th.call("DELETE", "/api/debates/"+id, "")
	}
	t.Setenv("GUARDIAN_MAX_WIKI_BUILDS", "")
	if cfg := debateConfigFromEnv(); cfg.MaxWikiBuilds != 1 {
		t.Errorf("default slots: %d", cfg.MaxWikiBuilds)
	}
	t.Setenv("GUARDIAN_MAX_WIKI_BUILDS", "3")
	if cfg := debateConfigFromEnv(); cfg.MaxWikiBuilds != 3 {
		t.Errorf("env slots: %d", cfg.MaxWikiBuilds)
	}
}

func TestWikiBuildDoesNotUseTheDebateLimit(t *testing.T) {
	th := newWikiHub(t, func(c *debateConfig) { c.MaxRunning = 1 })
	rec := th.call("POST", "/api/projects/slow-a/wiki/build", `{"stale":true}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("build: %d %s", rec.Code, rec.Body.String())
	}
	wikiID := decode(t, rec)["id"].(string)
	// The only debate slot is still free ...
	deb := th.call("POST", "/api/debates", `{"task":"one"}`)
	if deb.Code != http.StatusAccepted {
		t.Fatalf("debate while a build runs: %d %s", deb.Code, deb.Body.String())
	}
	debID := decode(t, deb)["id"].(string)
	// ... and a running debate does not take the wiki slot or block another debate check.
	if rec = th.call("POST", "/api/debates", `{"task":"two"}`); rec.Code != http.StatusTooManyRequests {
		t.Errorf("second debate: %d", rec.Code)
	}
	th.call("DELETE", "/api/debates/"+wikiID, "")
	th.call("DELETE", "/api/debates/"+debID, "")
}

func TestWikiJobsInDebateListAndEvents(t *testing.T) {
	th := newWikiHub(t, nil)
	rec := th.call("POST", "/api/projects/proj/wiki/build", `{"stale":true}`)
	id := decode(t, rec)["id"].(string)
	th.waitDone(t, id)

	rec = th.call("GET", "/api/debates", "")
	items, _ := decode(t, rec)["debates"].([]interface{})
	if len(items) != 1 {
		t.Fatalf("list: %s", rec.Body.String())
	}
	item := items[0].(map[string]interface{})
	if item["id"] != id || item["kind"] != "code_wiki" || item["events"] != true || item["project"] != "proj" {
		t.Errorf("list item: %v", item)
	}
	rec = th.call("GET", "/api/debates/"+id+"/events", "")
	evs, _ := decode(t, rec)["events"].([]interface{})
	if rec.Code != 200 || len(evs) != 1 || evs[0].(map[string]interface{})["type"] != "wiki_build_started" {
		t.Errorf("events: %d %s", rec.Code, rec.Body.String())
	}
	// A project filter on the list applies to wiki jobs too.
	rec = th.call("GET", "/api/debates?project=other", "")
	if items, _ = decode(t, rec)["debates"].([]interface{}); len(items) != 0 {
		t.Errorf("filtered list: %s", rec.Body.String())
	}
	// A debate job carries no kind.
	deb := th.call("POST", "/api/debates", `{"task":"one"}`)
	if _, has := decode(t, deb)["kind"]; has {
		t.Errorf("debate summary has a kind: %s", deb.Body.String())
	}
	th.call("DELETE", "/api/debates/"+decode(t, deb)["id"].(string), "")
}

func TestWikiRoutesNeedTheCLIAndAuth(t *testing.T) {
	th := newWikiHub(t, func(c *debateConfig) { c.CLI = filepath.Join(t.TempDir(), "missing.py") })
	for _, rt := range []struct{ method, path string }{
		{"GET", "/api/projects/p/wiki"}, {"GET", "/api/projects/p/wiki/x"}, {"PUT", "/api/projects/p/wiki/x"},
		{"DELETE", "/api/projects/p/wiki/x"}, {"POST", "/api/projects/p/wiki/build"},
	} {
		if rec := th.call(rt.method, rt.path, `{}`); rec.Code != http.StatusServiceUnavailable {
			t.Errorf("%s %s without CLI: %d", rt.method, rt.path, rec.Code)
		}
	}
	th = newWikiHub(t, nil)
	h := withAuth(th.h, testToken)
	for _, rt := range []struct{ method, path string }{
		{"GET", "/api/projects/p/wiki"}, {"PUT", "/api/projects/p/wiki/x"}, {"POST", "/api/projects/p/wiki/build"},
	} {
		if rec := do(h, rt.method, rt.path, `{}`, nil); rec.Code != http.StatusUnauthorized {
			t.Errorf("%s %s without token: %d", rt.method, rt.path, rec.Code)
		}
	}
}

func TestWikiRoutesIgnoreUIActionsGate(t *testing.T) {
	// GUARDIAN_UI_ACTIONS=off blocks the action/thread decision routes only; wiki edits and builds
	// are like persona/team/project saves and debate starts, which it does not cover.
	th := newWikiHub(t, func(c *debateConfig) { c.UIActions = "off" })
	if rec := th.call("PUT", "/api/projects/p/wiki/x", `{"title":"T","scope":[],"body":"b"}`); rec.Code != 200 {
		t.Errorf("save with UI actions off: %d %s", rec.Code, rec.Body.String())
	}
	if rec := th.call("DELETE", "/api/projects/p/wiki/x", ""); rec.Code != 200 {
		t.Errorf("delete with UI actions off: %d", rec.Code)
	}
}

package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeSoloCLI mimics guardian.py --agent-run. Projects named busy/bad select the early exits 4/2; a question
// containing "slow" keeps the run going for a while, "crash" exits 1 with no result; anything else stores
// a result.json in the record directory the hub made (<data>/solo-runs/<id>), as the real CLI does.
const fakeSoloCLI = `#!/bin/sh
dir=$(dirname "$0")
printf '%s\n' "$@" > "$dir/last-args.txt"
q=""; id=""; project=""; general=""
while [ $# -gt 0 ]; do
  case "$1" in
    --agent-run=*) q="${1#--agent-run=}" ;;
    --solo-id=*) id="${1#--solo-id=}" ;;
    --project=*) project="${1#--project=}" ;;
    --general) general=1 ;;
  esac
  shift
done
case "$project" in
  busy*) echo '{"ok": false, "error": "busy: 2 solo run(s) already running (GUARDIAN_MAX_SOLO)"}'; exit 4 ;;
  bad*) echo '{"ok": false, "error": "project: unknown project bad"}'; exit 2 ;;
esac
case "$q" in
  *crash*) echo boom >&2; exit 1 ;;
  *slow*) sleep 3 ;;
esac
rec="$dir/data/solo-runs/$id"
mkdir -p "$rec"
if [ -n "$general" ]; then
  printf '{"id":"%s","label":"SOLO","note":"unverified, no Judge, no sources","mode":"general","project":"","role":"domain_architect","provider":"claude","question":"%s","answer":"a","degraded":false,"summary":"no sources","created":"2026-10-30T10:00:00+00:00","elapsed_s":1.5}\n' "$id" "$q" > "$rec/result.json"
  echo '{"ok": true}'
  exit 0
fi
printf '{"id":"%s","label":"SOLO","note":"unverified, no Judge","project":"%s","role":"domain_architect","provider":"claude","question":"%s","answer":"a","degraded":false,"summary":"1 verified, 0 unshown, 0 missing","created":"2026-10-30T10:00:00+00:00","elapsed_s":1.5}\n' "$id" "${project:-default}" "$q" > "$rec/result.json"
echo '{"ok": true}'
`

func newSoloHub(t *testing.T, mutate func(*debateConfig)) *testHub {
	t.Helper()
	dir := t.TempDir()
	cli := filepath.Join(dir, "fake-guardian.sh")
	if err := os.WriteFile(cli, []byte(fakeSoloCLI), 0o755); err != nil {
		t.Fatal(err)
	}
	cfg := debateConfig{
		CLI: cli, Python: "sh", DataDir: filepath.Join(dir, "data"),
		MaxRunning: 2, JobTimeout: time.Minute, ConfigLimit: 10 * time.Second,
		MaxSolo: 2, SoloStartGrace: 400 * time.Millisecond,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	hub := newDebateHub(cfg)
	mux := http.NewServeMux()
	hub.register(mux)
	th := &testHub{hub: hub, h: mux, dir: dir, cli: cli}
	// Runs before the directory removal: wait until every child has exited and its goroutine is done.
	t.Cleanup(func() {
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			hub.mu.Lock()
			n := len(hub.soloRuns)
			hub.mu.Unlock()
			if n == 0 {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	})
	return th
}

// waitSolo polls GET /api/solo/{id} until the run is no longer running.
func (th *testHub) waitSolo(t *testing.T, id string) map[string]interface{} {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		m := decode(t, th.call("GET", "/api/solo/"+id, ""))
		if m["status"] != "running" {
			return m
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("solo run %s did not finish", id)
	return nil
}

func TestSoloStartRunsTheCLIAndStoresAResult(t *testing.T) {
	th := newSoloHub(t, nil)
	rec := th.call("POST", "/api/solo", `{"question":"how does auth work?","project":"server-guardian","role":"domain_architect","provider":"claude","language":"Korean","context_paths":["apps/**/*.go"],"context_ref":"main"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	m := decode(t, rec)
	id, _ := m["id"].(string)
	if !soloIDPattern.MatchString(id) || m["label"] != "SOLO" || m["note"] != "unverified, no Judge" {
		t.Fatalf("unexpected start answer: %v", m)
	}
	done := th.waitSolo(t, id)
	if done["status"] != "completed" {
		t.Fatalf("status: %v", done)
	}
	res, _ := done["result"].(map[string]interface{})
	if res["id"] != id || res["label"] != "SOLO" || res["question"] != "how does auth work?" {
		t.Fatalf("result: %v", res)
	}
	want := []string{"--agent-run=how does auth work?", "--solo-id=" + id, "--json", "--project=server-guardian",
		"--role=domain_architect", "--provider=claude", "--language=Korean", "--context-path=apps/**/*.go", "--context-ref=main"}
	// newCmd puts the CLI path first; the fake script is run by sh, so $0 is not in the argument list.
	if got := th.lastArgs(t); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("argv:\n%q\nwant:\n%q", got, want)
	}
	// A solo run is no debate job.
	if list := decode(t, th.call("GET", "/api/debates", "")); len(list["debates"].([]interface{})) != 0 {
		t.Fatalf("solo run leaked into the debate list: %v", list)
	}
}

func TestSoloGeneralQuestionPassesTheFlagAndKeepsItsOwnLabel(t *testing.T) {
	th := newSoloHub(t, nil)
	rec := th.call("POST", "/api/solo", `{"question":"what is a mutex?","general":true,"role":"domain_architect","provider":"claude"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	m := decode(t, rec)
	id, _ := m["id"].(string)
	if m["label"] != "SOLO" || m["note"] != "unverified, no Judge, no sources" {
		t.Fatalf("start answer: %v", m)
	}
	done := th.waitSolo(t, id)
	if done["status"] != "completed" || done["note"] != "unverified, no Judge, no sources" {
		t.Fatalf("view: %v", done)
	}
	want := []string{"--agent-run=what is a mutex?", "--solo-id=" + id, "--json", "--general", "--role=domain_architect", "--provider=claude"}
	if got := th.lastArgs(t); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("argv:\n%q\nwant:\n%q", got, want)
	}
	row := decode(t, th.call("GET", "/api/solo", ""))["runs"].([]interface{})[0].(map[string]interface{})
	if row["mode"] != "general" || row["note"] != "unverified, no Judge, no sources" || row["project"] != nil {
		t.Errorf("list row: %v", row)
	}
}

func TestSoloStartRejectsInvalidRequests(t *testing.T) {
	th := newSoloHub(t, nil)
	cases := map[string]string{
		"empty question":     `{"question":"  "}`,
		"missing question":   `{"project":"server-guardian"}`,
		"unknown field":      `{"question":"q","team":"x"}`,
		"not json":           `question`,
		"bad project":        `{"question":"q","project":"Bad Project"}`,
		"bad role":           `{"question":"q","role":"../x"}`,
		"bad provider":       `{"question":"q","provider":"a b"}`,
		"bad language":       `{"question":"q","language":"Korean; rm"}`,
		"dash context path":  `{"question":"q","context_paths":["--exec"]}`,
		"bad context ref":    `{"question":"q","context_ref":"-x"}`,
		"too long":           `{"question":"` + strings.Repeat("a", maxTaskChars+1) + `"}`,
		"nul in question":    `{"question":"a\u0000b"}`,
		"general + project":  `{"question":"q","general":true,"project":"server-guardian"}`,
		"general + paths":    `{"question":"q","general":true,"context_paths":["a"]}`,
		"general + ref":      `{"question":"q","general":true,"context_ref":"main"}`,
		"too many ctx paths": `{"question":"q","context_paths":[` + strings.Repeat(`"a",`, maxContextPaths) + `"a"]}`,
	}
	for name, body := range cases {
		if rec := th.call("POST", "/api/solo", body); rec.Code != http.StatusBadRequest {
			t.Errorf("%s: want 400, got %d %s", name, rec.Code, rec.Body.String())
		}
	}
	if entries, _ := os.ReadDir(th.hub.soloDir()); len(entries) != 0 {
		t.Fatalf("a rejected request stored a record: %v", entries)
	}
	big := `{"question":"` + strings.Repeat("a", soloBodyLimit) + `"}`
	if rec := th.call("POST", "/api/solo", big); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body: want 413, got %d", rec.Code)
	}
}

func TestSoloCLIRefusalsMapToHTTPErrorsAndStoreNothing(t *testing.T) {
	th := newSoloHub(t, nil)
	rec := th.call("POST", "/api/solo", `{"question":"q","project":"busy"}`)
	if rec.Code != http.StatusTooManyRequests || !strings.Contains(rec.Body.String(), "busy: 2 solo run") {
		t.Fatalf("exit 4: %d %s", rec.Code, rec.Body.String())
	}
	rec = th.call("POST", "/api/solo", `{"question":"q","project":"bad"}`)
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "unknown project") {
		t.Fatalf("exit 2: %d %s", rec.Code, rec.Body.String())
	}
	if entries, _ := os.ReadDir(th.hub.soloDir()); len(entries) != 0 {
		t.Fatalf("a refused run left a record: %v", entries)
	}
}

func TestSoloSlotsAreCountedBeforeSpawning(t *testing.T) {
	th := newSoloHub(t, func(c *debateConfig) { c.MaxSolo = 1 })
	first := th.call("POST", "/api/solo", `{"question":"slow one"}`)
	if first.Code != http.StatusAccepted {
		t.Fatalf("first: %d %s", first.Code, first.Body.String())
	}
	id := decode(t, first)["id"].(string)
	if m := decode(t, th.call("GET", "/api/solo/"+id, "")); m["status"] != "running" {
		t.Fatalf("expected running: %v", m)
	}
	second := th.call("POST", "/api/solo", `{"question":"another"}`)
	if second.Code != http.StatusTooManyRequests || !strings.Contains(second.Body.String(), "max 1") {
		t.Fatalf("second: %d %s", second.Code, second.Body.String())
	}
	if done := th.waitSolo(t, id); done["status"] != "completed" {
		t.Fatalf("slow run: %v", done)
	}
	// The slot is free again. A result file appears before the child exits, so the slot may be released a moment
	// after the run reads as completed: retry the 429 briefly.
	code, body := 0, ""
	for i := 0; i < 50; i++ {
		again := th.call("POST", "/api/solo", `{"question":"fast"}`)
		code, body = again.Code, again.Body.String()
		if code != http.StatusTooManyRequests {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if code != http.StatusAccepted {
		t.Fatalf("after: %d %s", code, body)
	}
}

func TestSoloFailedRunShowsTheCLIOutput(t *testing.T) {
	th := newSoloHub(t, nil)
	rec := th.call("POST", "/api/solo", `{"question":"crash now"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	done := th.waitSolo(t, decode(t, rec)["id"].(string))
	if done["status"] != "failed" || !strings.Contains(done["error"].(string), "boom") || done["result"] != nil {
		t.Fatalf("failed run: %v", done)
	}
}

func TestSoloTimeoutKillsTheRunAndSaysSo(t *testing.T) {
	th := newSoloHub(t, func(c *debateConfig) { c.JobTimeout = 300 * time.Millisecond; c.SoloStartGrace = 50 * time.Millisecond })
	rec := th.call("POST", "/api/solo", `{"question":"slow forever"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	done := th.waitSolo(t, decode(t, rec)["id"].(string))
	if done["status"] != "failed" || !strings.Contains(done["error"].(string), "timed out") {
		t.Fatalf("timeout: %v", done)
	}
}

func TestSoloGetValidatesTheIDAndDoesNotFollowSymlinks(t *testing.T) {
	th := newSoloHub(t, nil)
	if rec := th.call("GET", "/api/solo/..%2Fsecret", ""); rec.Code != http.StatusBadRequest {
		t.Errorf("traversal: %d", rec.Code)
	}
	if rec := th.call("GET", "/api/solo/solo_1", ""); rec.Code != http.StatusBadRequest {
		t.Errorf("short id: %d", rec.Code)
	}
	if rec := th.call("GET", "/api/solo/solo_20261030_101500_abc123", ""); rec.Code != http.StatusNotFound {
		t.Errorf("unknown id: %d", rec.Code)
	}
	root := th.hub.soloDir()
	outside := filepath.Join(th.dir, "outside")
	if err := os.MkdirAll(outside, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outside, "result.json"), []byte(`{"secret":true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	linked := "solo_20261030_101500_aaaaaa"
	if err := os.Symlink(outside, filepath.Join(root, linked)); err != nil {
		t.Fatal(err)
	}
	if rec := th.call("GET", "/api/solo/"+linked, ""); rec.Code != http.StatusNotFound {
		t.Errorf("symlinked record: %d %s", rec.Code, rec.Body.String())
	}
	// A result.json that is a symlink is not read either: the run shows as failed.
	real := "solo_20261030_101501_bbbbbb"
	if err := os.MkdirAll(filepath.Join(root, real), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(outside, "result.json"), filepath.Join(root, real, "result.json")); err != nil {
		t.Fatal(err)
	}
	if m := decode(t, th.call("GET", "/api/solo/"+real, "")); m["status"] != "failed" || m["result"] != nil {
		t.Errorf("symlinked result: %v", m)
	}
	list := decode(t, th.call("GET", "/api/solo", ""))["runs"].([]interface{})
	if len(list) != 1 || list[0].(map[string]interface{})["id"] != real {
		t.Errorf("list should skip the symlinked record: %v", list)
	}
}

func TestSoloListIsNewestFirstWithRowFacts(t *testing.T) {
	th := newSoloHub(t, nil)
	if m := decode(t, th.call("GET", "/api/solo", "")); len(m["runs"].([]interface{})) != 0 {
		t.Fatalf("empty list: %v", m)
	}
	root := th.hub.soloDir()
	ids := []string{"solo_20261029_090000_aaaaaa", "solo_20261030_090000_bbbbbb", "solo_20261030_100000_cccccc"}
	for _, id := range ids[:2] {
		dir := filepath.Join(root, id)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		body := `{"id":"` + id + `","project":"p","role":"r","provider":"claude","question":"` + strings.Repeat("q", 300) + `","summary":"s","degraded":true,"created":"c","elapsed_s":2}`
		if err := os.WriteFile(filepath.Join(dir, "result.json"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(root, ids[2]), 0o700); err != nil { // no result, no process: failed
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "not-a-run"), 0o700); err != nil {
		t.Fatal(err)
	}
	runs := decode(t, th.call("GET", "/api/solo", ""))["runs"].([]interface{})
	if len(runs) != 3 {
		t.Fatalf("runs: %v", runs)
	}
	got := []string{}
	for _, r := range runs {
		got = append(got, r.(map[string]interface{})["id"].(string))
	}
	if strings.Join(got, ",") != ids[2]+","+ids[1]+","+ids[0] {
		t.Fatalf("order: %v", got)
	}
	first := runs[0].(map[string]interface{})
	if first["status"] != "failed" || first["error"] == "" || first["label"] != "SOLO" {
		t.Errorf("failed row: %v", first)
	}
	row := runs[1].(map[string]interface{})
	q, _ := row["question"].(string)
	if row["status"] != "completed" || row["project"] != "p" || row["degraded"] != true || !strings.HasSuffix(q, "…") || len([]rune(q)) != soloListQuestionChar+1 {
		t.Errorf("completed row: %v", row)
	}
}

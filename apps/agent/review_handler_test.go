package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// fakeReviewCLI mimics guardian.py --review. Projects named busy/bad select the early exits 4/2; slow keeps the
// run going, crash exits 1 with no result; anything else stores a result.json in the record directory the hub
// made (<data>/reviews/<id>), as the real CLI does. It copies a given patch file next to the result so the test
// can see what reached the child.
const fakeReviewCLI = `#!/bin/sh
dir=$(dirname "$0")
printf '%s\n' "$@" > "$dir/last-args.txt"
id=""; project=""; patch=""; range=""; ref=""; action=""
while [ $# -gt 0 ]; do
  case "$1" in
    --review-id=*) id="${1#--review-id=}" ;;
    --project=*) project="${1#--project=}" ;;
    --patch=*) patch="${1#--patch=}" ;;
    --range=*) range="${1#--range=}" ;;
    --ref=*) ref="${1#--ref=}" ;;
    --action=*) action="${1#--action=}" ;;
  esac
  shift
done
case "$project" in
  busy*) echo '{"ok": false, "error": "busy: 2 review(s) already running (GUARDIAN_MAX_REVIEWS)"}'; exit 4 ;;
  bad*) echo '{"ok": false, "error": "range: the change is empty"}'; exit 2 ;;
  crash*) echo boom >&2; exit 1 ;;
  slow*) sleep 3 ;;
esac
rec="$dir/data/reviews/$id"
mkdir -p "$rec"
if [ -n "$patch" ]; then cp "$patch" "$rec/seen.diff"; change='{"patch":true}'; source='{"kind":"patch"}';
elif [ -n "$ref" ]; then change="{}"; source="{\"kind\":\"ref\",\"ref\":\"$ref\"}";
elif [ -n "$action" ]; then change="{}"; source="{\"kind\":\"action\",\"action_id\":$action}";
else change="{\"range\":\"$range\"}"; source="{\"kind\":\"range\",\"range\":\"$range\"}"; fi
printf '{"id":"%s","label":"REVIEW","note":"advisory, no Judge","state":"done","project":"%s","change":%s,"source":%s,"head":"abc","role":"critic_redteam","provider":"claude-cli","files":["a.py","b.py"],"findings":[{"severity":"high"},{"severity":"low"},{"severity":"low"}],"notes":[{}],"summary":"3 findings (1 high, 0 medium, 2 low), 1 notes","created":"2026-11-23T10:00:00+00:00","elapsed_s":2.5}\n' "$id" "$project" "$change" "$source" > "$rec/result.json"
echo '{"ok": true}'
`

func newReviewHub(t *testing.T, mutate func(*debateConfig)) *testHub {
	t.Helper()
	dir := t.TempDir()
	cli := filepath.Join(dir, "fake-guardian.sh")
	if err := os.WriteFile(cli, []byte(fakeReviewCLI), 0o755); err != nil {
		t.Fatal(err)
	}
	cfg := debateConfig{
		CLI: cli, Python: "sh", DataDir: filepath.Join(dir, "data"),
		MaxRunning: 2, JobTimeout: time.Minute, ConfigLimit: 10 * time.Second,
		MaxReviews: 2, SoloStartGrace: 400 * time.Millisecond,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	hub := newDebateHub(cfg)
	mux := http.NewServeMux()
	hub.register(mux)
	th := &testHub{hub: hub, h: mux, dir: dir, cli: cli}
	t.Cleanup(func() {
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			hub.mu.Lock()
			n := len(hub.reviewRuns)
			hub.mu.Unlock()
			if n == 0 {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	})
	return th
}

func (th *testHub) waitReview(t *testing.T, id string) map[string]interface{} {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		m := decode(t, th.call("GET", "/api/reviews/"+id, ""))
		if m["status"] != "running" {
			return m
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("review %s did not finish", id)
	return nil
}

func TestReviewStartRunsTheCLIWithARange(t *testing.T) {
	th := newReviewHub(t, nil)
	rec := th.call("POST", "/api/reviews", `{"project":"server-guardian","range":"main~1..main","role":"critic_redteam","provider":"claude","language":"Korean"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	m := decode(t, rec)
	id, _ := m["id"].(string)
	if !reviewIDPattern.MatchString(id) || m["label"] != "REVIEW" || m["note"] != "advisory, no Judge" {
		t.Fatalf("unexpected start answer: %v", m)
	}
	done := th.waitReview(t, id)
	res, _ := done["result"].(map[string]interface{})
	if done["status"] != "completed" || res["id"] != id {
		t.Fatalf("review: %v", done)
	}
	want := []string{"--review", "--review-id=" + id, "--project=server-guardian", "--json", "--range=main~1..main",
		"--role=critic_redteam", "--provider=claude", "--language=Korean"}
	if got := th.lastArgs(t); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("argv:\n%q\nwant:\n%q", got, want)
	}
}

func TestReviewPatchIsStoredPrivatelyAndPassedAsAFile(t *testing.T) {
	th := newReviewHub(t, nil)
	patch := "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
	rec := th.call("POST", "/api/reviews", `{"project":"server-guardian","patch":`+jsonString(patch)+`,"patch_base":"main"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	id, _ := decode(t, rec)["id"].(string)
	th.waitReview(t, id)
	recDir := filepath.Join(th.dir, "data", "reviews", id)
	stored := filepath.Join(recDir, reviewPatchFile)
	fi, err := os.Stat(stored)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("patch file: %v %v", fi, err)
	}
	if seen, _ := os.ReadFile(filepath.Join(recDir, "seen.diff")); string(seen) != patch {
		t.Fatalf("the child saw %q", seen)
	}
	args := strings.Join(th.lastArgs(t), "\n")
	if !strings.Contains(args, "--patch="+stored) || !strings.Contains(args, "--patch-base=main") || strings.Contains(args, "--range") {
		t.Fatalf("argv: %q", args)
	}
}

func TestReviewRequestShapeIsChecked(t *testing.T) {
	th := newReviewHub(t, nil)
	for _, body := range []string{
		`{}`,
		`{"project":"server-guardian"}`,
		`{"project":"server-guardian","range":"a..b","patch":"x"}`,
		`{"project":"server-guardian","range":"a...b"}`,
		`{"project":"server-guardian","range":"-x..b"}`,
		`{"project":"server-guardian","range":"main"}`,
		`{"project":"server-guardian","range":"a..b","patch_base":"main"}`,
		`{"project":"server-guardian","patch":"x","patch_base":"--evil"}`,
		`{"project":"../x","range":"a..b"}`,
		`{"project":"server-guardian","range":"a..b","role":"x y"}`,
		`{"project":"server-guardian","range":"a..b","unknown":1}`,
		// V2: exactly one of range, patch, ref and action; base and expect_sha only with ref.
		`{"project":"server-guardian","ref":"feature/x","range":"a..b"}`,
		`{"project":"server-guardian","ref":"feature/x","patch":"x"}`,
		`{"project":"server-guardian","ref":"feature/x","action":3}`,
		`{"project":"server-guardian","action":3,"range":"a..b"}`,
		`{"project":"server-guardian","action":-2}`,
		`{"project":"server-guardian","base":"main","range":"a..b"}`,
		`{"project":"server-guardian","expect_sha":"` + strings.Repeat("a", 40) + `","range":"a..b"}`,
		`{"project":"server-guardian","ref":"-evil"}`,
		`{"project":"server-guardian","ref":"feature/x..main"}`,
		`{"project":"server-guardian","ref":"feature x"}`,
		`{"project":"server-guardian","ref":"feature/x/","base":"main"}`,
		`{"project":"server-guardian","ref":"feature/x","base":"--evil"}`,
		`{"project":"server-guardian","ref":"feature/x","expect_sha":"abc"}`,
		`{"project":"server-guardian","ref":"feature/x","expect_sha":"` + strings.Repeat("A", 40) + `"}`,
		`{"project":"server-guardian","ref":"feature/x","started_by":"operator"}`,
		`{"project":"server-guardian","ref":"feature/x","started_by":"session:"}`,
	} {
		if rec := th.call("POST", "/api/reviews", body); rec.Code != http.StatusBadRequest {
			t.Errorf("%s: %d %s", body, rec.Code, rec.Body.String())
		}
	}
	if entries, _ := os.ReadDir(filepath.Join(th.dir, "data", "reviews")); len(entries) != 0 {
		t.Fatalf("refused requests left records: %v", entries)
	}
	// The new sources alone are accepted.
	for _, body := range []string{
		`{"project":"server-guardian","ref":"feature/x"}`,
		`{"project":"server-guardian","ref":"feature/x","base":"origin/main","expect_sha":"` + strings.Repeat("a", 40) +
			`","started_by":"session:mac-1"}`,
		`{"project":"server-guardian","action":7}`,
	} {
		rec := th.call("POST", "/api/reviews", body)
		if rec.Code != http.StatusAccepted {
			t.Fatalf("%s: %d %s", body, rec.Code, rec.Body.String())
		}
		th.waitReview(t, decode(t, rec)["id"].(string))
	}
	big := `{"project":"server-guardian","patch":"` + strings.Repeat("a", reviewPatchLimit+1) + `"}`
	if rec := th.call("POST", "/api/reviews", big); rec.Code != http.StatusBadRequest && rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized patch: %d", rec.Code)
	}
}

func TestReviewRequestJSONIsStoredAndShown(t *testing.T) {
	th := newReviewHub(t, nil)
	sha := strings.Repeat("ab", 20)
	rec := th.call("POST", "/api/reviews", `{"project":"server-guardian","ref":"feature/x","base":"origin/main",`+
		`"expect_sha":"`+sha+`","started_by":"session:ws-1"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("start: %d %s", rec.Code, rec.Body.String())
	}
	id := decode(t, rec)["id"].(string)
	reqPath := filepath.Join(th.dir, "data", "reviews", id, reviewRequestFile)
	fi, err := os.Stat(reqPath)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("request.json: %v %v", fi, err)
	}
	var stored map[string]interface{}
	if err := json.Unmarshal(mustRead(t, reqPath), &stored); err != nil {
		t.Fatalf("request.json is not JSON: %v", err)
	}
	want := map[string]interface{}{
		"project": "server-guardian",
		"source": map[string]interface{}{
			"kind": "ref", "ref": "feature/x", "base": "origin/main", "expect_sha": sha,
		},
		"started_by": "session:ws-1",
	}
	if !reflect.DeepEqual(stored, want) {
		t.Fatalf("request.json:\n%v\nwant:\n%v", stored, want)
	}
	done := th.waitReview(t, id)
	viewReq, _ := done["request"].(map[string]interface{})
	if done["status"] != "completed" || viewReq == nil || viewReq["started_by"] != "session:ws-1" {
		t.Fatalf("view: %v", done)
	}
	// An action review names its action; started_by may stay empty.
	rec = th.call("POST", "/api/reviews", `{"project":"server-guardian","action":12}`)
	id2 := decode(t, rec)["id"].(string)
	th.waitReview(t, id2)
	var stored2 map[string]interface{}
	if err := json.Unmarshal(mustRead(t, filepath.Join(th.dir, "data", "reviews", id2, reviewRequestFile)), &stored2); err != nil {
		t.Fatalf("request.json is not JSON: %v", err)
	}
	source2, _ := stored2["source"].(map[string]interface{})
	if source2["kind"] != "action" || source2["action_id"] != float64(12) || stored2["started_by"] != "" {
		t.Fatalf("action request.json: %v", stored2)
	}
	// The view omits "request" when the file is missing (records from before V2).
	if err := os.Remove(filepath.Join(th.dir, "data", "reviews", id2, reviewRequestFile)); err != nil {
		t.Fatal(err)
	}
	view := decode(t, th.call("GET", "/api/reviews/"+id2, ""))
	if _, has := view["request"]; has {
		t.Fatalf("view without request.json still has one: %v", view)
	}
}

func TestReviewListShowsTheSourceWhileRunningAndFiltersByAction(t *testing.T) {
	th := newReviewHub(t, nil)
	slow := th.call("POST", "/api/reviews", `{"project":"slow-p","ref":"feature/x","started_by":"session:mac-1"}`)
	if slow.Code != http.StatusAccepted {
		t.Fatalf("slow start: %d %s", slow.Code, slow.Body.String())
	}
	slowID := decode(t, slow)["id"].(string)
	rows := decode(t, th.call("GET", "/api/reviews", ""))["runs"].([]interface{})
	if len(rows) != 1 {
		t.Fatalf("list while running: %v", rows)
	}
	row, _ := rows[0].(map[string]interface{})
	if row["status"] != "running" || row["source_kind"] != "ref" || row["ref"] != "feature/x" ||
		row["started_by"] != "session:mac-1" || row["project"] != "slow-p" {
		t.Fatalf("running row: %v", row)
	}
	if _, has := row["action_id"]; has {
		t.Fatalf("running row has an action id: %v", row)
	}
	one := decode(t, th.call("GET", "/api/reviews?project=slow-p", ""))["runs"].([]interface{})
	if len(one) != 1 {
		t.Fatalf("project filter while running: %v", one)
	}
	// Two action reviews and one range review; ?action= keeps only its own.
	first := decode(t, th.call("POST", "/api/reviews", `{"project":"server-guardian","action":7}`))["id"].(string)
	th.waitReview(t, first)
	second := decode(t, th.call("POST", "/api/reviews", `{"project":"server-guardian","action":8}`))["id"].(string)
	th.waitReview(t, second)
	third := decode(t, th.call("POST", "/api/reviews", `{"project":"server-guardian","range":"a..b"}`))["id"].(string)
	th.waitReview(t, third)
	th.waitReview(t, slowID)
	filtered := decode(t, th.call("GET", "/api/reviews?action=7", ""))["runs"].([]interface{})
	if len(filtered) != 1 || filtered[0].(map[string]interface{})["id"] != first ||
		filtered[0].(map[string]interface{})["action_id"] != float64(7) {
		t.Fatalf("?action=7: %v", filtered)
	}
	both := decode(t, th.call("GET", "/api/reviews?project=server-guardian", ""))["runs"].([]interface{})
	if len(both) != 3 {
		t.Fatalf("project list: %v", both)
	}
	for _, bad := range []string{"0", "-1", "abc"} {
		if rec := th.call("GET", "/api/reviews?action="+bad, ""); rec.Code != http.StatusBadRequest {
			t.Fatalf("?action=%s: %d %s", bad, rec.Code, rec.Body.String())
		}
	}
}

// mustRead reads a small test file or fails the test.
func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestReviewEarlyRefusalsMapToStatusAndLeaveNoRecord(t *testing.T) {
	th := newReviewHub(t, nil)
	for project, status := range map[string]int{"bad-p": http.StatusBadRequest, "busy-p": http.StatusTooManyRequests} {
		rec := th.call("POST", "/api/reviews", `{"project":"`+project+`","range":"a..b"}`)
		if rec.Code != status || !strings.Contains(rec.Body.String(), "error") {
			t.Errorf("%s: %d %s", project, rec.Code, rec.Body.String())
		}
	}
	if entries, _ := os.ReadDir(filepath.Join(th.dir, "data", "reviews")); len(entries) != 0 {
		t.Fatalf("refused runs left records: %v", entries)
	}
}

func TestReviewSlotsAndFailedRuns(t *testing.T) {
	th := newReviewHub(t, func(c *debateConfig) { c.MaxReviews = 1 })
	first := th.call("POST", "/api/reviews", `{"project":"slow-p","range":"a..b"}`)
	if first.Code != http.StatusAccepted {
		t.Fatalf("first: %d %s", first.Code, first.Body.String())
	}
	if second := th.call("POST", "/api/reviews", `{"project":"server-guardian","range":"a..b"}`); second.Code != http.StatusTooManyRequests {
		t.Fatalf("second: %d %s", second.Code, second.Body.String())
	}
	th.waitReview(t, decode(t, first)["id"].(string))
	crash := th.call("POST", "/api/reviews", `{"project":"crash-p","range":"a..b"}`)
	id, _ := decode(t, crash)["id"].(string)
	done := th.waitReview(t, id)
	if done["status"] != "failed" || !strings.Contains(done["error"].(string), "boom") {
		t.Fatalf("crash: %v", done)
	}
}

func TestReviewGetAndListReadOnlyRealRecords(t *testing.T) {
	th := newReviewHub(t, nil)
	for _, p := range []string{"server-guardian", "other"} {
		rec := th.call("POST", "/api/reviews", `{"project":"`+p+`","range":"a..b"}`)
		th.waitReview(t, decode(t, rec)["id"].(string))
	}
	// A symlinked record and a stray name are never read.
	base := filepath.Join(th.dir, "data", "reviews")
	_ = os.Symlink(th.dir, filepath.Join(base, "review_20990101_000000_aaaaaa"))
	_ = os.MkdirAll(filepath.Join(base, "notareview"), 0o700)
	if rec := th.call("GET", "/api/reviews/review_20990101_000000_aaaaaa", ""); rec.Code != http.StatusNotFound {
		t.Fatalf("symlink: %d", rec.Code)
	}
	if rec := th.call("GET", "/api/reviews/../x", ""); rec.Code == http.StatusOK {
		t.Fatalf("bad id answered 200")
	}
	if rec := th.call("GET", "/api/reviews/review_1", ""); rec.Code != http.StatusBadRequest {
		t.Fatalf("bad id: %d", rec.Code)
	}
	all := decode(t, th.call("GET", "/api/reviews", ""))["runs"].([]interface{})
	if len(all) != 2 {
		t.Fatalf("list: %v", all)
	}
	row := all[0].(map[string]interface{})
	counts, _ := row["counts"].(map[string]interface{})
	if row["state"] != "done" || row["range"] != "a..b" || row["files"] != float64(2) || counts["high"] != float64(1) ||
		counts["low"] != float64(2) || counts["notes"] != float64(1) {
		t.Fatalf("row: %v", row)
	}
	one := decode(t, th.call("GET", "/api/reviews?project=other", ""))["runs"].([]interface{})
	if len(one) != 1 || one[0].(map[string]interface{})["project"] != "other" {
		t.Fatalf("filtered: %v", one)
	}
	if rec := th.call("GET", "/api/reviews?project=../x", ""); rec.Code != http.StatusBadRequest {
		t.Fatalf("bad filter: %d", rec.Code)
	}
}

// jsonString is s as a JSON string literal.
func jsonString(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

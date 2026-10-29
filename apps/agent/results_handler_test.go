package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

const (
	resThreadA = "thread_20261020_090000_aaa111"
	resThreadB = "thread_20261021_090000_bbb222"
	resThreadC = "thread_20261022_090000_ccc333"
)

// reportMarkdown is the head of what debate_report.py writes (title line plus the facts table).
func reportMarkdown(title, status string, rounds int) string {
	return "# 토론 결과: " + title + "\n\n| 항목 | 값 |\n| --- | --- |\n| 프로젝트 | `p` |\n| 결정 상태 | " + status +
		" |\n| 업데이트 | 2026-10-20 |\n| 라운드 | " + strconv.Itoa(rounds) + " |\n\n## 요약\n"
}

func (f *artFixture) report(t *testing.T, project, thread, body string, mtime time.Time) {
	t.Helper()
	p := filepath.Join(f.th.projDir(t, project), thread+".md")
	writeF(t, p, body)
	if err := os.Chtimes(p, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}

func resultItems(t *testing.T, f *artFixture, project string) []map[string]interface{} {
	t.Helper()
	rec := f.get("/api/projects/" + project + "/results")
	if rec.Code != 200 {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	raw := decode(t, rec)["items"].([]interface{})
	out := make([]map[string]interface{}, len(raw))
	for i, r := range raw {
		out[i] = r.(map[string]interface{})
	}
	return out
}

func TestResultsListReportsNewestFirst(t *testing.T) {
	f := newArtifactHub(t)
	base := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	f.report(t, "alpha", resThreadA, reportMarkdown("Old question", "APPROVED (승인)", 1), base)
	f.report(t, "alpha", resThreadB, reportMarkdown("New question", "DISPUTED (judge 승인, jev 게이트 미통과; 조치 보류)", 3), base.Add(48*time.Hour))
	f.report(t, "beta", resThreadC, reportMarkdown("Other project", "APPROVED (승인)", 1), base.Add(72*time.Hour))

	items := resultItems(t, f, "alpha")
	if len(items) != 2 {
		t.Fatalf("want 2 items, got %v", items)
	}
	first, second := items[0], items[1]
	if first["thread_id"] != resThreadB || second["thread_id"] != resThreadA {
		t.Fatalf("not newest first: %v", items)
	}
	if first["title"] != "New question" || first["status"] != "DISPUTED" || first["rounds"].(float64) != 3 {
		t.Fatalf("parsed fields: %v", first)
	}
	if first["has_report"] != true || first["artifact_versions"].(float64) != 0 {
		t.Fatalf("flags: %v", first)
	}
	if second["status"] != "APPROVED" {
		t.Fatalf("status: %v", second)
	}
}

func TestResultsMergeReportAndArtifactsByThread(t *testing.T) {
	f := newArtifactHub(t)
	base := time.Date(2026, 10, 20, 9, 0, 0, 0, time.UTC)
	f.report(t, "arch", resThreadA, reportMarkdown("Design talk", "APPROVED (승인)", 2), base)
	f.version(t, "arch", resThreadA, 1, map[string]string{"a.md": "# A\n"})
	f.version(t, "arch", resThreadA, 2, map[string]string{"a.md": "# A2\n", "b.md": "# B\n"})
	f.version(t, "arch", resThreadB, 1, map[string]string{"c.md": "# C\n"}) // artifacts without a report

	items := resultItems(t, f, "arch")
	if len(items) != 2 {
		t.Fatalf("want 2 merged items, got %v", items)
	}
	by := map[string]map[string]interface{}{}
	for _, it := range items {
		by[it["thread_id"].(string)] = it
	}
	a := by[resThreadA]
	if a["has_report"] != true || a["artifact_versions"].(float64) != 2 || a["artifact_files"].(float64) != 2 {
		t.Fatalf("merged item: %v", a)
	}
	b := by[resThreadB]
	if b["has_report"] != false || b["artifact_versions"].(float64) != 1 || b["title"] != resThreadB {
		t.Fatalf("artifact-only item: %v", b)
	}
}

func TestResultsToleratesOddReports(t *testing.T) {
	f := newArtifactHub(t)
	now := time.Now()
	f.report(t, "alpha", resThreadA, "no heading at all\n", now)
	f.report(t, "alpha", resThreadB, "# 토론 결과: "+strings.Repeat("긴", 400)+"\x1b[31m\n| 결정 상태 | <script>x</script> |\n", now)
	items := resultItems(t, f, "alpha")
	by := map[string]map[string]interface{}{}
	for _, it := range items {
		by[it["thread_id"].(string)] = it
	}
	if by[resThreadA]["title"] != resThreadA || by[resThreadA]["status"] != "" {
		t.Fatalf("fallbacks: %v", by[resThreadA])
	}
	title := by[resThreadB]["title"].(string)
	if len([]rune(title)) > 121 || strings.ContainsRune(title, 0x1b) {
		t.Fatalf("title not bounded or cleaned: %q", title)
	}
	if by[resThreadB]["status"] != "" {
		t.Fatalf("a status outside [A-Z_] must be dropped: %v", by[resThreadB]["status"])
	}
}

func TestResultsIgnoresStrangeEntries(t *testing.T) {
	f := newArtifactHub(t)
	dir := f.th.projDir(t, "alpha")
	now := time.Now()
	f.report(t, "alpha", resThreadA, reportMarkdown("Good", "APPROVED (승인)", 1), now)
	writeF(t, filepath.Join(dir, "notes.md"), "# not a thread\n")                 // not a thread id
	writeF(t, filepath.Join(dir, resThreadB+".txt"), "x")                         // wrong extension
	if err := os.Mkdir(filepath.Join(dir, resThreadC+".md"), 0o700); err != nil { // a directory named like a report
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "secret.md")
	writeF(t, outside, "# 토론 결과: leaked\n")
	if err := os.Symlink(outside, filepath.Join(dir, "thread_20261023_090000_ddd444.md")); err != nil {
		t.Fatal(err)
	}
	items := resultItems(t, f, "alpha")
	if len(items) != 1 || items[0]["thread_id"] != resThreadA {
		t.Fatalf("only the real report may be listed: %v", items)
	}
}

// A FIFO named like a report must be skipped without being opened (an open would block forever).
func TestResultsIgnoresFifo(t *testing.T) {
	f := newArtifactHub(t)
	dir := f.th.projDir(t, "alpha")
	f.report(t, "alpha", resThreadA, reportMarkdown("Good", "APPROVED (승인)", 1), time.Now())
	if err := syscall.Mkfifo(filepath.Join(dir, resThreadB+".md"), 0o600); err != nil {
		t.Skipf("mkfifo unavailable: %v", err)
	}
	done := make(chan []map[string]interface{}, 1)
	go func() { done <- resultItems(t, f, "alpha") }()
	select {
	case items := <-done:
		if len(items) != 1 || items[0]["thread_id"] != resThreadA {
			t.Fatalf("the FIFO must not be listed: %v", items)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("listing hung on a FIFO")
	}
}

func TestResultsNeedAuth(t *testing.T) {
	f := newArtifactHub(t)
	f.report(t, "alpha", resThreadA, reportMarkdown("Good", "APPROVED (승인)", 1), time.Now())
	chain := withSecurity(withAuth(f.th.h, testToken), securityConfig{allowedHosts: map[string]bool{"example.com": true}})
	p := "/api/projects/alpha/results"
	if rec := do(chain, "GET", p, "", nil); rec.Code != http.StatusUnauthorized {
		t.Errorf("without credentials: %d", rec.Code)
	}
	if rec := do(chain, "GET", p+"?access_token="+testToken, "", nil); rec.Code != http.StatusUnauthorized {
		t.Errorf("with a query token: %d, want 401", rec.Code)
	}
}

func TestResultsEmptyAndValidation(t *testing.T) {
	f := newArtifactHub(t)
	if items := resultItems(t, f, "nothing"); len(items) != 0 {
		t.Fatalf("unknown project lists nothing: %v", items)
	}
	if rec := f.get("/api/projects/BAD/results"); rec.Code != 400 {
		t.Fatalf("invalid id: %d", rec.Code)
	}
	if got := f.get("/api/projects/nothing/results").Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control: %q", got)
	}
}

func TestResultsAreCapped(t *testing.T) {
	f := newArtifactHub(t)
	dir := f.th.projDir(t, "alpha")
	base := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < maxResultItems+5; i++ {
		name := "thread_" + strings.Repeat("0", 6) + "_" + string(rune('a'+i%26)) + string(rune('a'+i/26%26)) + string(rune('a'+i/676))
		p := filepath.Join(dir, name+".md")
		writeF(t, p, reportMarkdown("t", "APPROVED (승인)", 1))
		mt := base.Add(time.Duration(i) * time.Minute)
		if err := os.Chtimes(p, mt, mt); err != nil {
			t.Fatal(err)
		}
	}
	rec := f.get("/api/projects/alpha/results")
	m := decode(t, rec)
	items := m["items"].([]interface{})
	if len(items) != maxResultItems || m["total"].(float64) != float64(maxResultItems+5) {
		t.Fatalf("items %d total %v", len(items), m["total"])
	}
	// The newest 200 are kept: the last written report (i = 204) leads, the 5 oldest (i = 0..4) are cut.
	newest := "thread_000000_" + string(rune('a'+204%26)) + string(rune('a'+204/26%26)) + string(rune('a'+204/676))
	oldest := "thread_000000_" + string(rune('a'+4%26)) + string(rune('a'+4/26%26)) + string(rune('a'+4/676))
	if got := items[0].(map[string]interface{})["thread_id"]; got != newest {
		t.Fatalf("first item %v, want %s", got, newest)
	}
	for _, it := range items {
		if it.(map[string]interface{})["thread_id"] == oldest {
			t.Fatalf("the oldest reports must be cut, found %s", oldest)
		}
	}
}

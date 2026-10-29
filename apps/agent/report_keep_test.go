package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func keepPath(thread string) string { return "/api/threads/" + thread + "/report/keep" }

func newKeepHub(t *testing.T, threads ...string) *testHub {
	t.Helper()
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	th := newTestHub(t, nil)
	d := th.projDir(t, "alpha")
	for _, thread := range threads {
		writeF(t, filepath.Join(d, thread+".md"), "# R\n")
	}
	return th
}

func TestReportKeepCallsTheCLIWithTheProjectOfTheReportAndAudits(t *testing.T) {
	th := newKeepHub(t, reportThread)
	rec := th.call("POST", keepPath(reportThread), `{}`)
	if rec.Code != 200 {
		t.Fatalf("keep: %d %s", rec.Code, rec.Body)
	}
	if got := decode(t, rec); got["sha256"] != "cccc" {
		t.Errorf("response: %v", got)
	}
	args, _ := os.ReadFile(filepath.Join(th.dir, "last-args.txt"))
	want := "--report-keep --project alpha --thread " + reportThread
	if got := strings.Join(strings.Fields(string(args)), " "); got != want {
		t.Errorf("CLI arguments: %q, want %q", got, want)
	}
	entries, err := readAuditTail(5)
	if err != nil || len(entries) != 1 {
		t.Fatalf("audit: %v %+v", err, entries)
	}
	e := entries[0]
	if e.Command != "report keep "+reportThread || e.Targets[0] != "alpha" || e.Status != "ok" ||
		e.Detail != "version=v1 sha256=cccc" || e.Actor == "" {
		t.Errorf("audit entry: %+v", e)
	}
}

func TestReportKeepRefusals(t *testing.T) {
	invalid, dup, crash := "thread_20261008_120000_inval0", "thread_20261008_120000_dup000", "thread_20261008_120000_crash0"
	th := newKeepHub(t, invalid, dup, crash)
	cases := []struct {
		name, thread string
		want         int
	}{
		{"bad thread id", "not-a-thread", 400},
		{"no report", "thread_20261008_120000_zzzzzz", 404},
		{"cli invalid", invalid, 400},
		{"cli already kept", dup, 409},
		{"cli crash", crash, 500},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if rec := th.call("POST", keepPath(c.thread), `{}`); rec.Code != c.want {
				t.Errorf("%d, want %d: %s", rec.Code, c.want, rec.Body)
			}
		})
	}
	if rec := th.call("GET", keepPath(invalid), ""); rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET keep: %d", rec.Code)
	}
}

func TestReportKeepNeedsCredentials(t *testing.T) {
	th := newKeepHub(t, reportThread)
	chain := withSecurity(withAuth(th.h, testToken), securityConfig{allowedHosts: map[string]bool{"example.com": true}})
	json := func(r *http.Request) { r.Header.Set("Content-Type", "application/json") }
	if rec := do(chain, "POST", keepPath(reportThread), `{}`, json); rec.Code != http.StatusUnauthorized {
		t.Errorf("without credentials: %d", rec.Code)
	}
	if rec := do(chain, "POST", keepPath(reportThread)+"?access_token="+testToken, `{}`, json); rec.Code != http.StatusUnauthorized {
		t.Errorf("with a query token: %d", rec.Code)
	}
	if rec := do(chain, "POST", keepPath(reportThread), `{}`, func(r *http.Request) {
		json(r)
		r.Header.Set("Authorization", "Bearer "+testToken)
	}); rec.Code != 200 {
		t.Errorf("with Bearer: %d %s", rec.Code, rec.Body)
	}
}

// A kept report is an ordinary artifact version: the read routes, the Results list and the conversion work on it for a
// project that is not a design project (the Go side has no kind gate; the CLI decides what may be written).
func TestKeptReportIsReadableAndConvertibleForACodeProject(t *testing.T) {
	f := newArtifactHub(t)
	const project = "alpha"
	f.report(t, project, resThreadA, reportMarkdown("Code review", "APPROVED (승인)", 1), time.Now())
	f.version(t, project, resThreadA, 1, map[string]string{"report.md": "# Code review\n"})

	items := resultItems(t, f, project)
	if len(items) != 1 || items[0]["has_report"] != true || items[0]["artifact_versions"].(float64) != 1 {
		t.Fatalf("results: %v", items)
	}
	base := "/api/threads/" + resThreadA + "/artifacts/v1"
	if rec := f.get(base); rec.Code != 200 {
		t.Errorf("manifest: %d %s", rec.Code, rec.Body)
	}
	if rec := f.get(base + "/files/report.md"); rec.Code != 200 || !strings.Contains(rec.Body.String(), "# Code review") {
		t.Errorf("file: %d %s", rec.Code, rec.Body)
	}
	if rec := f.th.call("POST", base+"/convert", `{"name":"report.md","format":"pptx"}`); rec.Code != 200 {
		t.Errorf("convert: %d %s", rec.Code, rec.Body)
	}
	args, _ := os.ReadFile(filepath.Join(f.th.dir, "last-args.txt"))
	if !strings.Contains(strings.Join(strings.Fields(string(args)), " "), "--project "+project) {
		t.Errorf("CLI arguments: %q", args)
	}
}

package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeFileCLI mimics the guardian.py file commands: a path containing "secret" is not readable (exit 3).
const fakeFileCLI = `#!/bin/sh
dir=$(dirname "$0")
printf '%s\n' "$@" > "$dir/last-args.txt"
mode=""; path=""
for a in "$@"; do
  case "$a" in
    --code-file-list) mode=list ;;
    --code-file-show=*) mode=show; path="${a#--code-file-show=}" ;;
    --code-file-read=*) mode=read; path="${a#--code-file-read=}" ;;
  esac
done
case "$path" in *secret*) echo '{"ok":false,"error":"not a readable file"}'; exit 3 ;; esac
case "$mode" in
  list) echo '{"ok":true,"project":"p","sha":"abc","files":[{"path":"a.go","size":3}],"truncated":false}' ;;
  show) printf '{"ok":true,"project":"p","sha":"abc","path":"%s","outline":[]}\n' "$path" ;;
  read) printf '{"ok":true,"project":"p","sha":"abc","path":"%s","start":1,"end":1,"total":1,"lines":["x"],"truncated":false}\n' "$path" ;;
esac
`

func newFileHub(t *testing.T) *testHub {
	t.Helper()
	cli := filepath.Join(t.TempDir(), "fake-files.sh")
	if err := os.WriteFile(cli, []byte(fakeFileCLI), 0o755); err != nil {
		t.Fatal(err)
	}
	th := newWikiHub(t, func(c *debateConfig) { c.CLI = cli })
	th.cli = cli
	return th
}

func fileLastArgs(t *testing.T, th *testHub) string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(th.cli), "last-args.txt"))
	if err != nil {
		t.Fatal(err)
	}
	return strings.Join(strings.Fields(string(raw)), " ")
}

func TestFileRoutesArgv(t *testing.T) {
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	th := newFileHub(t)
	cases := []struct{ path, argv string }{
		{"/api/projects/p/files?sha=abc123", "--code-file-list --project=p --json --sha=abc123"},
		{"/api/projects/p/files/show?path=cli/llm-agent/wiki_build.py", "--code-file-show=cli/llm-agent/wiki_build.py --project=p --json"},
		{"/api/projects/p/files/lines?path=a.go&start=5&end=9", "--code-file-read=a.go --start=5 --end=9 --project=p --json"},
		{"/api/projects/p/files/lines?path=a.go", "--code-file-read=a.go --start=1 --end=400 --project=p --json"},
		{"/api/projects/p/files/lines?path=a.go&start=10", "--code-file-read=a.go --start=10 --end=409 --project=p --json"},
	}
	for _, c := range cases {
		rec := th.call("GET", c.path, "")
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: %d %s", c.path, rec.Code, rec.Body.String())
		}
		if got := fileLastArgs(t, th); got != c.argv {
			t.Errorf("%s argv: %q, want %q", c.path, got, c.argv)
		}
	}
}

func TestFileRoutesValidation(t *testing.T) {
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	th := newFileHub(t)
	bad := []string{
		"/api/projects/BAD/files",
		"/api/projects/p/files?sha=-x",
		"/api/projects/p/files/show",
		"/api/projects/p/files/show?path=/etc/passwd",
		"/api/projects/p/files/show?path=../x",
		"/api/projects/p/files/show?path=a/./b",
		"/api/projects/p/files/show?path=a//b",
		"/api/projects/p/files/show?path=-x",
		"/api/projects/p/files/show?path=a%0Ab",
		"/api/projects/p/files/show?path=" + strings.Repeat("a", maxFilePathBytes+1),
		"/api/projects/p/files/lines?path=a.go&start=0",
		"/api/projects/p/files/lines?path=a.go&start=x",
		"/api/projects/p/files/lines?path=a.go&start=5&end=4",
		"/api/projects/p/files/lines?path=a.go&start=1&end=401",
	}
	for _, p := range bad {
		if rec := th.call("GET", p, ""); rec.Code != http.StatusBadRequest {
			t.Errorf("%s: %d, want 400 (%s)", p, rec.Code, rec.Body.String())
		}
	}
	if rec := th.call("GET", "/api/projects/p/files/show?path=config/secret.yaml", ""); rec.Code != http.StatusNotFound {
		t.Errorf("unreadable path: %d, want 404", rec.Code)
	}
}

func TestFileLinesAreAudited(t *testing.T) {
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	th := newFileHub(t)
	th.call("GET", "/api/projects/p/files/lines?path=a.go&start=2&end=3", "")
	th.call("GET", "/api/projects/p/files/lines?path=config/secret.yaml", "")
	th.call("GET", "/api/projects/p/files/show?path=a.go", "")  // not audited: no source
	th.call("GET", "/api/projects/p/files/lines?path=../x", "") // refused before the CLI: not audited
	entries, err := readAuditTail(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("audit entries: %+v", entries)
	}
	newest, oldest := entries[0], entries[1]
	if oldest.Route != routeFileLines || oldest.Command != "read a.go:2-3" || oldest.Status != "ok" ||
		len(oldest.Targets) != 1 || oldest.Targets[0] != "p" {
		t.Errorf("ok entry: %+v", oldest)
	}
	if newest.Command != "read config/secret.yaml:1-400" || newest.Status != "error" || newest.Detail != "not a readable file" ||
		newest.ExitCodes["p"] != 3 {
		t.Errorf("refused entry: %+v", newest)
	}
}

func TestFileRoutesNeedTheCLIAndAuth(t *testing.T) {
	th := newWikiHub(t, func(c *debateConfig) { c.CLI = filepath.Join(t.TempDir(), "missing.py") })
	for _, p := range []string{"/api/projects/p/files", "/api/projects/p/files/show?path=a", "/api/projects/p/files/lines?path=a"} {
		if rec := th.call("GET", p, ""); rec.Code != http.StatusServiceUnavailable {
			t.Errorf("%s without CLI: %d", p, rec.Code)
		}
	}
	th = newFileHub(t)
	h := withAuth(th.h, testToken)
	for _, p := range []string{"/api/projects/p/files", "/api/projects/p/files/show?path=a", "/api/projects/p/files/lines?path=a"} {
		if rec := do(h, "GET", p, "", nil); rec.Code != http.StatusUnauthorized {
			t.Errorf("%s without token: %d", p, rec.Code)
		}
	}
}

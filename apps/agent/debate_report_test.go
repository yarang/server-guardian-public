package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const reportThread = "thread_20261008_120000_abc123"

func (th *testHub) projDir(t *testing.T, project string) string {
	t.Helper()
	d := filepath.Join(th.dir, "data", "decisions", "projects", project)
	if err := os.MkdirAll(d, 0o700); err != nil {
		t.Fatal(err)
	}
	return d
}

func writeF(t *testing.T, p, c string) {
	t.Helper()
	if err := os.WriteFile(p, []byte(c), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestThreadReportHappyPath(t *testing.T) {
	th := newTestHub(t, nil)
	d := th.projDir(t, "alpha")
	writeF(t, filepath.Join(d, reportThread+".md"), "# R\n![s](x.assets/summary.svg)")
	ad := filepath.Join(d, reportThread+".assets")
	os.MkdirAll(ad, 0o700)
	writeF(t, filepath.Join(ad, "b.svg"), "<svg>b</svg>")
	writeF(t, filepath.Join(ad, "a.svg"), "<svg>a</svg>")
	writeF(t, filepath.Join(ad, "bad.txt"), "no")
	rec := th.call("GET", "/api/threads/"+reportThread+"/report", "")
	if rec.Code != 200 {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	if rec.Header().Get("Cache-Control") != "no-store" {
		t.Error("cache header")
	}
	m := decode(t, rec)
	if m["project"] != "alpha" || m["thread_id"] != reportThread || !strings.HasPrefix(m["markdown"].(string), "# R") {
		t.Fatalf("%v", m)
	}
	assets := m["assets"].([]interface{})
	if len(assets) != 2 || assets[0].(map[string]interface{})["name"] != "a.svg" {
		t.Fatalf("assets %v", assets)
	}
	rec = th.call("GET", "/api/threads/"+reportThread+"/report/assets/a.svg", "")
	if rec.Code != 200 || decode(t, rec)["text"] != "<svg>a</svg>" {
		t.Fatalf("asset %d %s", rec.Code, rec.Body)
	}
}

func TestThreadReportNoAssetsFolderAndMissing(t *testing.T) {
	th := newTestHub(t, nil)
	if rec := th.call("GET", "/api/threads/"+reportThread+"/report", ""); rec.Code != 404 {
		t.Fatalf("missing: %d", rec.Code)
	}
	writeF(t, filepath.Join(th.projDir(t, "alpha"), reportThread+".md"), "x")
	m := decode(t, th.call("GET", "/api/threads/"+reportThread+"/report", ""))
	if a, ok := m["assets"].([]interface{}); !ok || len(a) != 0 {
		t.Fatalf("assets %v", m["assets"])
	}
	if rec := th.call("GET", "/api/threads/"+reportThread+"/report/assets/a.svg", ""); rec.Code != 404 {
		t.Fatalf("asset: %d", rec.Code)
	}
}

func TestThreadReportNewestProject(t *testing.T) {
	th := newTestHub(t, nil)
	p1 := filepath.Join(th.projDir(t, "alpha"), reportThread+".md")
	p2 := filepath.Join(th.projDir(t, "beta"), reportThread+".md")
	writeF(t, p1, "old")
	writeF(t, p2, "new")
	now := time.Now()
	os.Chtimes(p1, now.Add(-time.Hour), now.Add(-time.Hour))
	os.Chtimes(p2, now, now)
	m := decode(t, th.call("GET", "/api/threads/"+reportThread+"/report", ""))
	if m["project"] != "beta" || m["markdown"] != "new" {
		t.Fatalf("%v", m)
	}
}

func TestThreadReportBadInput(t *testing.T) {
	th := newTestHub(t, nil)
	if rec := th.call("GET", "/api/threads/bad!/report", ""); rec.Code != 400 {
		t.Fatalf("thread: %d", rec.Code)
	}
	for _, n := range []string{"..%2Fx.svg", "X.svg", "a.txt", "-a.svg"} {
		rec := th.call("GET", "/api/threads/"+reportThread+"/report/assets/"+n, "")
		if rec.Code != 400 && rec.Code != 404 {
			t.Errorf("%s: %d", n, rec.Code)
		}
		if n != "..%2Fx.svg" && rec.Code != 400 {
			t.Errorf("%s: want 400 got %d", n, rec.Code)
		}
	}
}

func TestThreadReportSymlinksNotServed(t *testing.T) {
	th := newTestHub(t, nil)
	secret := filepath.Join(th.dir, "secret")
	os.MkdirAll(secret, 0o700)
	writeF(t, filepath.Join(secret, "s.md"), "TOPSECRET")
	writeF(t, filepath.Join(secret, "a.svg"), "TOPSECRET")
	d := th.projDir(t, "alpha")
	// symlinked md
	os.Symlink(filepath.Join(secret, "s.md"), filepath.Join(d, reportThread+".md"))
	rec := th.call("GET", "/api/threads/"+reportThread+"/report", "")
	if rec.Code != 404 || strings.Contains(rec.Body.String(), "TOPSECRET") {
		t.Fatalf("md symlink: %d %s", rec.Code, rec.Body)
	}
	// real md, symlinked assets dir
	os.Remove(filepath.Join(d, reportThread+".md"))
	writeF(t, filepath.Join(d, reportThread+".md"), "ok")
	os.Symlink(secret, filepath.Join(d, reportThread+".assets"))
	rec = th.call("GET", "/api/threads/"+reportThread+"/report", "")
	if len(decode(t, rec)["assets"].([]interface{})) != 0 || strings.Contains(rec.Body.String(), "TOPSECRET") {
		t.Fatalf("assets dir symlink listed: %s", rec.Body)
	}
	rec = th.call("GET", "/api/threads/"+reportThread+"/report/assets/a.svg", "")
	if rec.Code != 404 || strings.Contains(rec.Body.String(), "TOPSECRET") {
		t.Fatalf("assets dir symlink: %d %s", rec.Code, rec.Body)
	}
	// real assets dir, symlinked asset
	os.Remove(filepath.Join(d, reportThread+".assets"))
	ad := filepath.Join(d, reportThread+".assets")
	os.MkdirAll(ad, 0o700)
	os.Symlink(filepath.Join(secret, "a.svg"), filepath.Join(ad, "a.svg"))
	rec = th.call("GET", "/api/threads/"+reportThread+"/report/assets/a.svg", "")
	if rec.Code != 404 || strings.Contains(rec.Body.String(), "TOPSECRET") {
		t.Fatalf("asset symlink: %d %s", rec.Code, rec.Body)
	}
	rec = th.call("GET", "/api/threads/"+reportThread+"/report", "")
	if len(decode(t, rec)["assets"].([]interface{})) != 0 {
		t.Fatalf("symlinked asset listed: %s", rec.Body)
	}
	// symlinked project dir
	os.Symlink(secret, filepath.Join(filepath.Dir(d), "linked"))
	writeF(t, filepath.Join(secret, "thread_20261009_000000_aaaaaa.md"), "TOPSECRET")
	rec = th.call("GET", "/api/threads/thread_20261009_000000_aaaaaa/report", "")
	if rec.Code != 404 {
		t.Fatalf("project symlink: %d", rec.Code)
	}
}

func TestThreadReportSizeCaps(t *testing.T) {
	th := newTestHub(t, nil)
	d := th.projDir(t, "alpha")
	writeF(t, filepath.Join(d, reportThread+".md"), strings.Repeat("a", maxReportMD+1))
	if rec := th.call("GET", "/api/threads/"+reportThread+"/report", ""); rec.Code != 413 {
		t.Fatalf("md: %d", rec.Code)
	}
	writeF(t, filepath.Join(d, reportThread+".md"), "ok")
	ad := filepath.Join(d, reportThread+".assets")
	os.MkdirAll(ad, 0o700)
	writeF(t, filepath.Join(ad, "big.svg"), strings.Repeat("a", maxReportAsset+1))
	if rec := th.call("GET", "/api/threads/"+reportThread+"/report/assets/big.svg", ""); rec.Code != 413 {
		t.Fatalf("asset: %d", rec.Code)
	}
}

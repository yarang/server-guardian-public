package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeUI(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"index.html":       "<html>dashboard</html>",
		"assets/app-1.js":  "console.log(1)",
		"assets/app-1.css": "body{}",
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func newUIMux(t *testing.T, dir string) *http.ServeMux {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/status", func(w http.ResponseWriter, r *http.Request) {
		jsonResponse(w, http.StatusOK, map[string]string{"status": "healthy"})
	})
	ok, err := registerUI(mux, dir)
	if err != nil || !ok {
		t.Fatalf("registerUI(%q) = %v, %v", dir, ok, err)
	}
	return mux
}

func TestUIHandler(t *testing.T) {
	mux := newUIMux(t, writeUI(t))
	cases := []struct {
		name, method, path string
		code               int
		bodyHas, cache     string
	}{
		{"root serves index", "GET", "/", 200, "dashboard", "no-cache"},
		{"asset is immutable", "GET", "/assets/app-1.js", 200, "console.log", "immutable"},
		{"spa fallback", "GET", "/fleet/nodes/site-a-arm1", 200, "dashboard", "no-cache"},
		{"api routes still win", "GET", "/api/status", 200, "healthy", ""},
		{"unknown api is 404", "GET", "/api/nope", 404, "unknown API endpoint", ""},
		{"dot-dot is redirected to a clean path", "GET", "/../../etc/passwd", 307, "", ""},
		{"host files are never served", "GET", "/etc/passwd", 200, "dashboard", "no-cache"},
		{"post rejected", "POST", "/", 405, "GET required", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			mux.ServeHTTP(rec, httptest.NewRequest(c.method, c.path, nil))
			if rec.Code != c.code {
				t.Fatalf("code = %d, want %d", rec.Code, c.code)
			}
			if !strings.Contains(rec.Body.String(), c.bodyHas) {
				t.Fatalf("body %q does not contain %q", rec.Body.String(), c.bodyHas)
			}
			if c.cache != "" && !strings.Contains(rec.Header().Get("Cache-Control"), c.cache) {
				t.Fatalf("Cache-Control = %q, want %q", rec.Header().Get("Cache-Control"), c.cache)
			}
		})
	}
}

func TestUISecurityHeaders(t *testing.T) {
	mux := newUIMux(t, writeUI(t))
	for _, p := range []string{"/", "/index.html", "/fleet/nodes/site-a-arm1", "/assets/app-1.js"} {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest("GET", p, nil))
		csp := rec.Header().Get("Content-Security-Policy")
		for _, d := range []string{"default-src 'self'", "script-src 'self';", "connect-src 'self'", "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "form-action 'self'"} {
			if !strings.Contains(csp, d) {
				t.Errorf("%s: CSP %q missing %q", p, csp, d)
			}
		}
		if strings.Contains(csp, "googleapis") || strings.Contains(csp, "gstatic") {
			t.Errorf("%s: CSP still allows external font hosts: %q", p, csp)
		}
		if strings.Contains(csp, "unsafe-eval") || strings.Contains(strings.SplitN(csp, "style-src", 2)[0], "unsafe-inline") {
			t.Errorf("%s: CSP allows inline/eval scripts: %q", p, csp)
		}
		if rec.Header().Get("X-Content-Type-Options") != "nosniff" || rec.Header().Get("Referrer-Policy") != "no-referrer" {
			t.Errorf("%s: missing nosniff/no-referrer: %v", p, rec.Header())
		}
	}
}

func TestRegisterUIDisabledOrMissing(t *testing.T) {
	mux := http.NewServeMux()
	if ok, err := registerUI(mux, ""); ok || err != nil {
		t.Fatalf("empty dir: got %v, %v; want disabled without error", ok, err)
	}
	if ok, err := registerUI(mux, t.TempDir()); ok || err == nil {
		t.Fatalf("dir without index.html: got %v, %v; want error", ok, err)
	}
}

package main

import (
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// dashboardCSP locks the served dashboard to its own scripts and API. Google Fonts are the
// only third-party resources (index.html); 'unsafe-inline' is limited to styles because the
// UI renders inline style attributes. There is no inline <script>, so script-src stays 'self'.
const dashboardCSP = "default-src 'self'; script-src 'self'; " +
	"style-src 'self' 'unsafe-inline'; " +
	"font-src 'self' data:; img-src 'self' data:; connect-src 'self'; " +
	"object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"

// setUISecurityHeaders applies the browser hardening headers to every dashboard response.
func setUISecurityHeaders(h http.Header) {
	h.Set("Content-Security-Policy", dashboardCSP)
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Referrer-Policy", "no-referrer")
}

// uiHandler serves the built web dashboard (apps/web-desktop/dist) from dir.
//
// Hashed files under /assets/ are cached for a year; everything else, including the
// SPA fallback to index.html for unknown paths, is revalidated on every request.
// Unknown /api/ paths return 404 instead of the dashboard.
func uiHandler(dir string) http.Handler {
	fileServer := http.FileServer(http.Dir(dir))
	index := filepath.Join(dir, "index.html")
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		setUISecurityHeaders(w.Header())
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET required"})
			return
		}
		clean := path.Clean("/" + r.URL.Path)
		if clean == "/api" || strings.HasPrefix(clean, "/api/") {
			jsonResponse(w, http.StatusNotFound, map[string]string{"error": "unknown API endpoint"})
			return
		}
		if strings.HasPrefix(clean, "/assets/") {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		} else {
			w.Header().Set("Cache-Control", "no-cache")
		}
		// http.Dir rejects paths escaping dir; stat the cleaned path for the SPA fallback.
		if info, err := os.Stat(filepath.Join(dir, filepath.FromSlash(clean))); err != nil || (info.IsDir() && clean != "/") {
			http.ServeFile(w, r, index)
			return
		}
		fileServer.ServeHTTP(w, r)
	})
}

// registerUI mounts the dashboard on mux when AGENT_UI_DIR points at a build with index.html.
// Only hosts that should serve the dashboard set it; others expose the API alone.
func registerUI(mux *http.ServeMux, dir string) (bool, error) {
	if dir == "" {
		return false, nil
	}
	if _, err := os.Stat(filepath.Join(dir, "index.html")); err != nil {
		return false, err
	}
	mux.Handle("/", uiHandler(dir))
	return true, nil
}

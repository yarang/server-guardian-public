package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestCheckAuthMode(t *testing.T) {
	cases := []struct {
		name, token, insecure string
		hosts                 []string
		ok                    bool
	}{
		{"token set, public bind", "tok", "", []string{"0.0.0.0"}, true},
		{"token set, insecure flag ignored", "tok", "1", []string{"100.64.0.7"}, true},
		{"no token, no opt-in", "", "", []string{"127.0.0.1"}, false},
		{"whitespace token counts as unset", "  ", "", []string{"127.0.0.1"}, false},
		{"opt-in must be exactly 1", "", "true", []string{"127.0.0.1"}, false},
		{"opt-in on IPv4 loopback", "", "1", []string{"127.0.0.1"}, true},
		{"opt-in on IPv6 loopback", "", "1", []string{"::1"}, true},
		{"opt-in on bracketed IPv6 loopback", "", "1", []string{"[::1]"}, true},
		{"opt-in on localhost", "", "1", []string{"localhost"}, true},
		{"opt-in on 127.0.0.2", "", "1", []string{"127.0.0.2"}, true},
		{"opt-in with a mesh address", "", "1", []string{"127.0.0.1", "100.64.0.7"}, false},
		{"opt-in on wildcard", "", "1", []string{"0.0.0.0"}, false},
		{"opt-in on IPv6 wildcard", "", "1", []string{"::"}, false},
		{"opt-in on a hostname", "", "1", []string{"site-a-arm1"}, false},
		{"opt-in without hosts", "", "1", nil, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := checkAuthMode(c.token, c.insecure, c.hosts)
			if (err == nil) != c.ok {
				t.Fatalf("checkAuthMode(%q, %q, %v) = %v, want ok=%v", c.token, c.insecure, c.hosts, err, c.ok)
			}
		})
	}
}

func testSecurityConfig() securityConfig {
	return newSecurityConfig("", "guardian.example.ts.net, Dash.Example.", "site-a-arm1.tail1234.ts.net")
}

func TestHostAllowed(t *testing.T) {
	cfg := testSecurityConfig()
	cases := []struct {
		host string
		want bool
	}{
		{"127.0.0.1:9870", true},
		{"100.64.0.7:9870", true},
		{"100.64.0.7", true},
		{"[::1]:9870", true},
		{"[::1]", true},
		{"[fe80::1%25eth0]:9870", true},
		{"localhost:9870", true},
		{"LOCALHOST.:9870", true},
		{"guardian.example.ts.net:9870", true},
		{"dash.example:9870", true},
		{"site-a-arm1:9870", true},
		{"site-a-arm1.tail1234.ts.net:9870", true},
		{"evil.example:9870", false},
		{"127.0.0.1.evil.example:9870", false},
		{"localhost.evil.example:9870", false},
		{"sub.localhost:9870", false},
		{"", false},
		{":9870", false},
	}
	for _, c := range cases {
		if got := cfg.hostAllowed(c.host); got != c.want {
			t.Errorf("hostAllowed(%q) = %v, want %v", c.host, got, c.want)
		}
	}
}

func TestOriginAllowed(t *testing.T) {
	cfg := testSecurityConfig()
	cases := []struct {
		origin, host string
		want         bool
	}{
		{"http://100.64.0.7:9870", "100.64.0.7:9870", true},
		{"http://[::1]:9870", "[::1]:9870", true},
		{"http://Site-A-ARM1:9870", "site-a-arm1:9870", true},
		{"tauri://localhost", "100.64.0.7:9870", true},
		{"http://tauri.localhost", "100.64.0.7:9870", true},
		{"http://localhost:5173", "127.0.0.1:9870", true},
		{"https://evil.example", "100.64.0.7:9870", false},
		{"http://100.64.0.7:9871", "100.64.0.7:9870", false},
		{"http://100.64.0.7", "100.64.0.7:9870", false},
		{"null", "100.64.0.7:9870", false},
		{"file://", "100.64.0.7:9870", false},
		{"tauri://evil", "100.64.0.7:9870", false},
		{"http://u@100.64.0.7:9870", "100.64.0.7:9870", false},
		{"http://100.64.0.7:9870/x", "100.64.0.7:9870", false},
	}
	for _, c := range cases {
		if got := cfg.originAllowed(c.origin, c.host); got != c.want {
			t.Errorf("originAllowed(%q, %q) = %v, want %v", c.origin, c.host, got, c.want)
		}
	}
}

func TestNewSecurityConfigRejectsWildcardAndNull(t *testing.T) {
	cfg := newSecurityConfig("*, null, https://ops.example/", "", "")
	if cfg.corsOrigins["*"] || cfg.corsOrigins["null"] {
		t.Fatalf("unsafe origins accepted: %v", cfg.corsOrigins)
	}
	if !cfg.corsOrigins["https://ops.example"] || cfg.corsOrigins["tauri://localhost"] {
		t.Fatalf("AGENT_CORS_ORIGINS must replace the defaults: %v", cfg.corsOrigins)
	}
}

// securedHandler is the production chain: withSecurity -> withAuth -> mux.
func securedHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/exec", func(w http.ResponseWriter, r *http.Request) {
		jsonResponse(w, http.StatusOK, map[string]string{"ok": "1"})
	})
	mux.HandleFunc("/api/fleet/exec/stream", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("POST /api/actions/{id}/run", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("dashboard")) })
	return withSecurity(withAuth(mux, testToken), testSecurityConfig())
}

func TestWithSecurity(t *testing.T) {
	h := securedHandler()
	type hdr map[string]string
	set := func(host string, headers hdr) func(*http.Request) {
		return func(r *http.Request) {
			r.Host = host
			for k, v := range headers {
				r.Header.Set(k, v)
			}
		}
	}
	const ip = "100.64.0.7:9870"
	bearer := "Bearer " + testToken
	cookie := sessionCookie + "=" + newSessionValue(testToken, time.Now())
	cases := []struct {
		name, method, path, body string
		mutate                   func(*http.Request)
		code                     int
		acao                     string
	}{
		{"preflight from tauri", "OPTIONS", "/api/exec", "", set(ip, hdr{"Origin": "tauri://localhost", "Access-Control-Request-Method": "POST"}), 204, "tauri://localhost"},
		{"preflight from foreign origin", "OPTIONS", "/api/exec", "", set(ip, hdr{"Origin": "https://evil.example"}), 403, ""},
		{"tauri POST with bearer", "POST", "/api/exec", `{"command":"id"}`, set(ip, hdr{"Origin": "tauri://localhost", "Content-Type": "application/json", "Authorization": bearer}), 200, "tauri://localhost"},
		{"foreign origin with valid bearer", "POST", "/api/exec", `{"command":"id"}`, set(ip, hdr{"Origin": "https://evil.example", "Content-Type": "application/json", "Authorization": bearer}), 403, ""},
		{"null origin", "POST", "/api/exec", `{"command":"id"}`, set(ip, hdr{"Origin": "null", "Content-Type": "application/json", "Authorization": bearer}), 403, ""},
		{"form-encoded POST", "POST", "/api/exec", "command=id", set(ip, hdr{"Content-Type": "application/x-www-form-urlencoded", "Authorization": bearer}), 415, ""},
		{"text/plain POST", "POST", "/api/exec", `{"command":"id"}`, set(ip, hdr{"Content-Type": "text/plain", "Authorization": bearer}), 415, ""},
		{"empty form POST to body-less action", "POST", "/api/actions/1/run", "", set(ip, hdr{"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie}), 415, ""},
		{"body-less POST without Content-Type", "POST", "/api/actions/1/run", "", set(ip, hdr{"Authorization": bearer}), 200, ""},
		{"curl without Origin, bearer", "POST", "/api/exec", `{"command":"id"}`, set(ip, hdr{"Content-Type": "application/json; charset=utf-8", "Authorization": bearer}), 200, ""},
		{"curl without Origin, no token", "POST", "/api/exec", `{"command":"id"}`, set(ip, hdr{"Content-Type": "application/json"}), 401, ""},
		{"same-origin dashboard POST with cookie", "POST", "/api/exec", `{"command":"id"}`, set(ip, hdr{"Origin": "http://100.64.0.7:9870", "Content-Type": "application/json", "Cookie": cookie, "Sec-Fetch-Site": "same-origin"}), 200, ""},
		{"same-origin dashboard POST stream", "POST", "/api/fleet/exec/stream", `{"command":"id"}`, set(ip, hdr{"Origin": "http://100.64.0.7:9870", "Content-Type": "application/json", "Cookie": cookie, "Sec-Fetch-Site": "same-origin"}), 200, ""},
		{"tauri POST stream with bearer", "POST", "/api/fleet/exec/stream", `{"command":"id"}`, set(ip, hdr{"Origin": "tauri://localhost", "Content-Type": "application/json", "Authorization": bearer}), 200, "tauri://localhost"},
		{"cross-site stream POST without Origin", "POST", "/api/fleet/exec/stream", `{"command":"id"}`, set("127.0.0.1:9870", hdr{"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json", "Authorization": bearer}), 403, ""},
		{"text/plain stream POST", "POST", "/api/fleet/exec/stream", `{"command":"id"}`, set(ip, hdr{"Content-Type": "text/plain", "Cookie": cookie}), 415, ""},
		{"foreign origin stream POST", "POST", "/api/fleet/exec/stream", `{"command":"id"}`, set(ip, hdr{"Origin": "https://evil.example", "Content-Type": "application/json", "Authorization": bearer}), 403, ""},
		{"DNS rebinding host", "POST", "/api/exec", `{"command":"id"}`, set("evil.example:9870", hdr{"Origin": "http://evil.example:9870", "Content-Type": "application/json", "Authorization": bearer}), 421, ""},
		{"rebinding host without origin", "GET", "/api/exec", "", set("evil.example:9870", hdr{"Authorization": bearer}), 421, ""},
		{"allowlisted hostname", "POST", "/api/exec", `{"command":"id"}`, set("guardian.example.ts.net:9870", hdr{"Origin": "http://guardian.example.ts.net:9870", "Content-Type": "application/json", "Authorization": bearer}), 200, ""},
		{"IPv6 host with port", "POST", "/api/exec", `{"command":"id"}`, set("[::1]:9870", hdr{"Origin": "http://[::1]:9870", "Content-Type": "application/json", "Authorization": bearer}), 200, ""},
		{"login behind security", "POST", "/api/auth/login", `{"token":"` + testToken + `"}`, set(ip, hdr{"Origin": "http://100.64.0.7:9870", "Content-Type": "application/json"}), 200, ""},
		{"login form-encoded", "POST", "/api/auth/login", "token=" + testToken, set(ip, hdr{"Content-Type": "application/x-www-form-urlencoded"}), 415, ""},
		{"logout behind security", "POST", "/api/auth/logout", "", set(ip, hdr{"Origin": "http://100.64.0.7:9870", "Sec-Fetch-Site": "same-origin"}), 200, ""},
		{"dashboard ignores API policy", "GET", "/", "", set("evil.example:9870", nil), 200, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := do(h, c.method, c.path, c.body, c.mutate)
			if rec.Code != c.code {
				t.Fatalf("code = %d, want %d (body %q)", rec.Code, c.code, rec.Body.String())
			}
			if got := rec.Header().Get("Access-Control-Allow-Origin"); got != c.acao {
				t.Fatalf("Access-Control-Allow-Origin = %q, want %q", got, c.acao)
			}
			if got := rec.Header().Get("Access-Control-Allow-Origin"); got == "*" {
				t.Fatal("wildcard CORS must never be sent")
			}
		})
	}
}

func TestWithSecurityPreflightHeaders(t *testing.T) {
	rec := do(securedHandler(), "OPTIONS", "/api/exec", "", func(r *http.Request) {
		r.Host = "127.0.0.1:9870"
		r.Header.Set("Origin", "http://localhost:5173")
	})
	if rec.Code != http.StatusNoContent {
		t.Fatalf("code = %d", rec.Code)
	}
	if m := rec.Header().Get("Access-Control-Allow-Methods"); !strings.Contains(m, "POST") || !strings.Contains(m, "DELETE") {
		t.Fatalf("Allow-Methods = %q", m)
	}
	if h := rec.Header().Get("Access-Control-Allow-Headers"); !strings.Contains(h, "Authorization") {
		t.Fatalf("Allow-Headers = %q", h)
	}
	if rec.Header().Get("Access-Control-Allow-Credentials") != "" {
		t.Fatal("credentials must not be allowed cross-origin")
	}
}

func TestLoginSetsCookieBehindSecurity(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/auth/login", strings.NewReader(`{"token":"`+testToken+`"}`))
	req.Host = "127.0.0.1:9870"
	req.Header.Set("Content-Type", "application/json")
	securedHandler().ServeHTTP(rec, req)
	if rec.Code != 200 || !strings.Contains(rec.Header().Get("Set-Cookie"), sessionCookie+"=") {
		t.Fatalf("login: code %d, Set-Cookie %q", rec.Code, rec.Header().Get("Set-Cookie"))
	}
}

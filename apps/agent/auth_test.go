package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const testToken = "s3cret-token-value"

func authMux() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/exec", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("/api/fleet/exec/stream", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("/api/stream/metrics", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("dashboard")) })
	return withAuth(mux, testToken)
}

func do(h http.Handler, method, target, body string, mutate func(*http.Request)) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	if mutate != nil {
		mutate(req)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestWithAuth(t *testing.T) {
	h := authMux()
	bearer := func(v string) func(*http.Request) {
		return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+v) }
	}
	cookie := func(v string) func(*http.Request) {
		return func(r *http.Request) { r.AddCookie(&http.Cookie{Name: sessionCookie, Value: v}) }
	}
	cases := []struct {
		name   string
		method string
		target string
		mutate func(*http.Request)
		code   int
	}{
		{"no credentials", "POST", "/api/exec", nil, 401},
		{"wrong bearer", "POST", "/api/exec", bearer("nope"), 401},
		{"valid bearer", "POST", "/api/exec", bearer(testToken), 200},
		{"raw token as cookie is rejected", "POST", "/api/exec", cookie(testToken), 401},
		{"valid session cookie", "POST", "/api/exec", cookie(newSessionValue(testToken, time.Now())), 200},
		{"query token on normal api is rejected", "POST", "/api/exec?access_token=" + testToken, nil, 401},
		{"query token on metrics stream", "GET", "/api/stream/metrics?access_token=" + testToken, nil, 200},
		{"wrong query token on metrics stream", "GET", "/api/stream/metrics?access_token=x", nil, 401},
		{"query token on command stream is rejected", "POST", "/api/fleet/exec/stream?access_token=" + testToken, nil, 401},
		{"dashboard stays public", "GET", "/", nil, 200},
		{"preflight allowed", "OPTIONS", "/api/exec", nil, 204},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if rec := do(h, c.method, c.target, "", c.mutate); rec.Code != c.code {
				t.Fatalf("code = %d, want %d", rec.Code, c.code)
			}
		})
	}
}

func TestLoginIssuesSessionCookie(t *testing.T) {
	h := authMux()
	if rec := do(h, "POST", "/api/auth/login", `{"token":"wrong"}`, nil); rec.Code != 401 {
		t.Fatalf("bad login code = %d", rec.Code)
	}
	rec := do(h, "POST", "/api/auth/login", `{"token":"`+testToken+`"}`, nil)
	if rec.Code != 200 {
		t.Fatalf("login code = %d", rec.Code)
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != sessionCookie || !cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteStrictMode {
		t.Fatalf("unexpected cookie: %+v", cookies)
	}
	if strings.Contains(cookies[0].Value, testToken) {
		t.Fatal("cookie must not contain the raw token")
	}
	got := do(h, "POST", "/api/exec", "", func(r *http.Request) { r.AddCookie(cookies[0]) })
	if got.Code != 200 {
		t.Fatalf("cookie auth code = %d", got.Code)
	}
	if rec := do(h, "GET", "/api/auth/login", "", nil); rec.Code != 405 {
		t.Fatalf("GET login code = %d", rec.Code)
	}
}

func TestEmptyTokenDisablesAuth(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/exec", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	if rec := do(withAuth(mux, ""), "POST", "/api/exec", "", nil); rec.Code != 200 {
		t.Fatalf("code = %d", rec.Code)
	}
}

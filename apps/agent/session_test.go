package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestSessionLifetime(t *testing.T) {
	store := &sessionStore{revoked: map[string]time.Time{}}
	now := time.Unix(1_800_000_000, 0)
	v := newSessionValue(testToken, now)

	if !store.valid(testToken, v, now.Add(sessionMaxAge-time.Second)) {
		t.Fatal("session must be valid until it expires")
	}
	if store.valid(testToken, v, now.Add(sessionMaxAge)) {
		t.Fatal("expired session accepted")
	}
	if store.valid(testToken, v, now.Add(-time.Hour)) {
		t.Fatal("session issued in the future accepted")
	}
	if store.valid("rotated-token", v, now) {
		t.Fatal("session survived a token rotation")
	}
	if newSessionValue(testToken, now) == v {
		t.Fatal("two logins must get different sessions")
	}
}

func TestSessionRejectsForgeries(t *testing.T) {
	store := &sessionStore{revoked: map[string]time.Time{}}
	now := time.Now()
	v := newSessionValue(testToken, now)
	parts := strings.Split(v, ".")

	// The v1 cookie was a fixed HMAC of the token with no expiry.
	mac := hmac.New(sha256.New, []byte(testToken))
	mac.Write([]byte("guardian-session-v1"))
	v1 := hex.EncodeToString(mac.Sum(nil))

	later := strings.Join([]string{"v2", "9999999999", parts[2], parts[3]}, ".")
	for name, bad := range map[string]string{
		"v1 static cookie":  v1,
		"extended expiry":   later,
		"changed nonce":     strings.Join([]string{"v2", parts[1], strings.Repeat("0", 32), parts[3]}, "."),
		"truncated":         strings.Join(parts[:3], "."),
		"raw token":         testToken,
		"empty":             "",
		"wrong version tag": "v3." + strings.Join(parts[1:], "."),
	} {
		if store.valid(testToken, bad, now) {
			t.Errorf("%s accepted", name)
		}
	}
}

func TestLogoutRevokesTheSession(t *testing.T) {
	h := authMux()
	login := do(h, "POST", "/api/auth/login", `{"token":"`+testToken+`"}`, nil)
	c := login.Result().Cookies()[0]
	if c.MaxAge != int(sessionMaxAge.Seconds()) || c.Secure {
		t.Fatalf("cookie MaxAge %d Secure %v", c.MaxAge, c.Secure)
	}
	other := do(h, "POST", "/api/auth/login", `{"token":"`+testToken+`"}`, nil).Result().Cookies()[0]
	withCookie := func(c *http.Cookie) func(*http.Request) { return func(r *http.Request) { r.AddCookie(c) } }

	if rec := do(h, "GET", "/api/auth/logout", "", withCookie(c)); rec.Code != 405 {
		t.Fatalf("GET logout code = %d, want 405", rec.Code)
	}
	if rec := do(h, "POST", "/api/exec", "", withCookie(c)); rec.Code != 200 {
		t.Fatalf("GET logout must not revoke; code = %d", rec.Code)
	}
	if rec := do(h, "POST", "/api/auth/logout", "", withCookie(c)); rec.Code != 200 {
		t.Fatalf("logout code = %d", rec.Code)
	}
	if rec := do(h, "POST", "/api/exec", "", withCookie(c)); rec.Code != 401 {
		t.Fatalf("revoked cookie code = %d, want 401", rec.Code)
	}
	if rec := do(h, "POST", "/api/exec", "", withCookie(other)); rec.Code != 200 {
		t.Fatalf("another session must survive; code = %d", rec.Code)
	}
}

func TestLoginOverTLSSetsSecure(t *testing.T) {
	rec := do(authMux(), "POST", "https://hub:9870/api/auth/login", `{"token":"`+testToken+`"}`, nil)
	if c := rec.Result().Cookies(); len(c) != 1 || !c[0].Secure {
		t.Fatalf("cookies over TLS: %+v", c)
	}
}

func TestRevokedNoncesArePruned(t *testing.T) {
	store := &sessionStore{revoked: map[string]time.Time{}}
	now := time.Now()
	store.revoke(testToken, newSessionValue(testToken, now.Add(-sessionMaxAge+time.Minute)), now)
	store.revoke(testToken, newSessionValue(testToken, now), now.Add(2*time.Minute))
	if len(store.revoked) != 1 {
		t.Fatalf("expired revocations kept: %d", len(store.revoked))
	}
}

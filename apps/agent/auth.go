package main

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	sessionCookie  = "guardian_session"
	sessionMaxAge  = 7 * 24 * time.Hour
	loginFailDelay = 500 * time.Millisecond
	// sessionSkew tolerates a clock that moved back slightly between issuing and checking.
	sessionSkew = 5 * time.Minute
)

// streamPaths may authenticate with ?access_token= because EventSource cannot send headers
// cross-origin (the Tauri app). Same-origin dashboards use the session cookie instead. Only
// read-only streams belong here: the fleet command stream is a POST read with fetch and
// takes the token in the Authorization header like every other command endpoint.
var streamPaths = map[string]bool{
	"/api/stream/metrics": true,
}

// A session cookie is "v2.<issued unix>.<nonce hex>.<mac hex>", MAC'd with the token. It
// expires sessionMaxAge after login, logout revokes its nonce, and rotating AGENT_TOKEN
// invalidates every session at once. The token itself is never stored in the browser.

// sessions remembers revoked nonces until their cookies would have expired anyway. It lives in
// memory: an agent restart forgets revocations, which sessionMaxAge bounds.
type sessionStore struct {
	mu      sync.Mutex
	revoked map[string]time.Time
}

var sessions = &sessionStore{revoked: map[string]time.Time{}}

func sessionMAC(token, issued, nonce string) string {
	mac := hmac.New(sha256.New, []byte(token))
	mac.Write([]byte("guardian-session-v2|" + issued + "|" + nonce))
	return hex.EncodeToString(mac.Sum(nil))
}

// newSessionValue issues a fresh cookie value for a login at now.
func newSessionValue(token string, now time.Time) string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("crypto/rand: %v", err))
	}
	issued := strconv.FormatInt(now.Unix(), 10)
	nonce := hex.EncodeToString(b)
	return "v2." + issued + "." + nonce + "." + sessionMAC(token, issued, nonce)
}

// parseSession returns the cookie's nonce and expiry when its MAC matches the token.
func parseSession(token, value string) (nonce string, expires time.Time, ok bool) {
	parts := strings.Split(value, ".")
	if len(parts) != 4 || parts[0] != "v2" || len(parts[2]) != 32 {
		return "", time.Time{}, false
	}
	if !secureEqual(parts[3], sessionMAC(token, parts[1], parts[2])) {
		return "", time.Time{}, false
	}
	issued, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil {
		return "", time.Time{}, false
	}
	return parts[2], time.Unix(issued, 0).Add(sessionMaxAge), true
}

func (s *sessionStore) valid(token, value string, now time.Time) bool {
	nonce, expires, ok := parseSession(token, value)
	if !ok || !now.Before(expires) || now.Add(sessionSkew).Before(expires.Add(-sessionMaxAge)) {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, revoked := s.revoked[nonce]
	return !revoked
}

func (s *sessionStore) revoke(token, value string, now time.Time) {
	nonce, expires, ok := parseSession(token, value)
	if !ok {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for n, exp := range s.revoked {
		if !now.Before(exp) {
			delete(s.revoked, n)
		}
	}
	if now.Before(expires) {
		s.revoked[nonce] = expires
	}
}

func secureEqual(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

// authorized reports whether the request carries the token as a bearer header, a valid
// session cookie, or (stream endpoints only) an access_token query parameter.
func authorized(r *http.Request, token string) bool {
	if auth := r.Header.Get("Authorization"); strings.HasPrefix(auth, "Bearer ") {
		return secureEqual(strings.TrimPrefix(auth, "Bearer "), token)
	}
	if c, err := r.Cookie(sessionCookie); err == nil && sessions.valid(token, c.Value, time.Now()) {
		return true
	}
	if streamPaths[r.URL.Path] {
		if q := r.URL.Query().Get("access_token"); q != "" {
			return secureEqual(q, token)
		}
	}
	return false
}

// @MX:ANCHOR: [AUTO] Authentication boundary for every /api route, including /api/exec.
// @MX:REASON: /api/exec and the fleet endpoints run shell commands on this host and over SSH on the fleet.
// withAuth protects /api/* with token. Static dashboard files stay public so the login page
// can load. An empty token disables authentication; checkAuthMode only allows that with
// GUARDIAN_INSECURE_NO_AUTH=1 on a loopback-only agent.
func withAuth(next http.Handler, token string) http.Handler {
	if token == "" {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		switch {
		case r.Method == http.MethodOptions:
			// withSecurity answers /api preflights (and sets CORS headers) before this point.
			w.WriteHeader(http.StatusNoContent)
		case path == "/api/auth/login":
			handleLogin(w, r, token)
		case path == "/api/auth/logout":
			handleLogout(w, r, token)
		case !strings.HasPrefix(path, "/api/") && path != "/api":
			next.ServeHTTP(w, r)
		case authorized(r, token):
			next.ServeHTTP(w, r)
		default:
			w.Header().Set("WWW-Authenticate", `Bearer realm="server-guardian"`)
			jsonResponse(w, http.StatusUnauthorized, map[string]string{"error": "authentication required"})
		}
	})
}

// handleLogin exchanges the token for an HttpOnly session cookie.
func handleLogin(w http.ResponseWriter, r *http.Request, token string) {
	if r.Method != http.MethodPost {
		jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	var req struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&req); err != nil || !secureEqual(req.Token, token) {
		time.Sleep(loginFailDelay)
		jsonResponse(w, http.StatusUnauthorized, map[string]string{"error": "invalid token"})
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name:     sessionCookie,
		Value:    newSessionValue(token, time.Now()),
		Path:     "/",
		MaxAge:   int(sessionMaxAge.Seconds()),
		HttpOnly: true,
		Secure:   r.TLS != nil,
		SameSite: http.SameSiteStrictMode,
	})
	jsonResponse(w, http.StatusOK, map[string]string{"status": "authenticated"})
}

// handleLogout revokes the presented session on the server and clears the cookie. POST only,
// so a link or image cannot sign the user out.
func handleLogout(w http.ResponseWriter, r *http.Request, token string) {
	if r.Method != http.MethodPost {
		jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	if c, err := r.Cookie(sessionCookie); err == nil {
		sessions.revoke(token, c.Value, time.Now())
	}
	http.SetCookie(w, &http.Cookie{Name: sessionCookie, Value: "", Path: "/", MaxAge: -1, HttpOnly: true, Secure: r.TLS != nil, SameSite: http.SameSiteStrictMode})
	jsonResponse(w, http.StatusOK, map[string]string{"status": "logged_out"})
}

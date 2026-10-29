package main

import (
	"errors"
	"fmt"
	"log"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"strings"
)

// defaultCORSOrigins are the cross-origin clients this repo ships: the Tauri desktop app
// (tauri://localhost on macOS/Linux, http(s)://tauri.localhost on Windows) and the Vite dev
// server (devUrl in tauri.conf.json and the default `npm run dev` host). AGENT_CORS_ORIGINS
// replaces the whole list.
var defaultCORSOrigins = []string{
	"tauri://localhost",
	"http://tauri.localhost",
	"https://tauri.localhost",
	"http://localhost:5173",
	"http://127.0.0.1:5173",
}

// securityConfig is the request-level policy applied in front of authentication.
type securityConfig struct {
	corsOrigins  map[string]bool
	allowedHosts map[string]bool
}

// splitList parses a comma-separated environment value into trimmed, non-empty entries.
func splitList(raw string) []string {
	var out []string
	for _, v := range strings.Split(raw, ",") {
		if v = strings.TrimSpace(v); v != "" {
			out = append(out, v)
		}
	}
	return out
}

// normalizeHostName lower-cases a host name and drops one trailing dot ("Foo.example." and
// "foo.example" are the same DNS name).
func normalizeHostName(h string) string {
	return strings.ToLower(strings.TrimSuffix(h, "."))
}

// newSecurityConfig builds the policy from AGENT_CORS_ORIGINS and AGENT_ALLOWED_HOSTS.
// The machine's own hostname (full and first label, which is also its MagicDNS short
// name) is always allowed so http://<hostname>:9870 keeps working.
//
// "*" and "null" are never accepted as CORS origins: "*" would re-open the API to every
// site, and "null" is the origin of sandboxed iframes, data: URLs and file:// pages, which
// any attacker can produce.
func newSecurityConfig(corsOrigins, allowedHosts, hostname string) securityConfig {
	cfg := securityConfig{corsOrigins: map[string]bool{}, allowedHosts: map[string]bool{}}
	origins := splitList(corsOrigins)
	if len(origins) == 0 {
		origins = defaultCORSOrigins
	}
	for _, o := range origins {
		o = strings.ToLower(strings.TrimSuffix(o, "/"))
		if o == "*" || o == "null" {
			log.Printf("ignoring unsafe AGENT_CORS_ORIGINS entry %q", o)
			continue
		}
		cfg.corsOrigins[o] = true
	}
	hosts := splitList(allowedHosts)
	if hostname != "" {
		hosts = append(hosts, hostname, strings.SplitN(hostname, ".", 2)[0])
	}
	for _, h := range hosts {
		if h = normalizeHostName(h); h != "" {
			cfg.allowedHosts[h] = true
		}
	}
	return cfg
}

func securityConfigFromEnv() securityConfig {
	hostname, _ := os.Hostname()
	return newSecurityConfig(os.Getenv("AGENT_CORS_ORIGINS"), os.Getenv("AGENT_ALLOWED_HOSTS"), hostname)
}

// parseIPLiteral parses an IPv4/IPv6 literal, with or without brackets and IPv6 zone.
func parseIPLiteral(h string) (netip.Addr, bool) {
	a, err := netip.ParseAddr(strings.TrimSuffix(strings.TrimPrefix(h, "["), "]"))
	return a, err == nil
}

// isLoopbackHost reports whether a listen address is "localhost" or a loopback IP.
func isLoopbackHost(h string) bool {
	if normalizeHostName(h) == "localhost" {
		return true
	}
	a, ok := parseIPLiteral(h)
	return ok && a.Unmap().IsLoopback()
}

// checkAuthMode fails closed: without AGENT_TOKEN the agent only starts when the operator
// opts in with GUARDIAN_INSECURE_NO_AUTH=1 and every listen address is loopback.
// A whitespace-only token counts as unset.
func checkAuthMode(token, insecure string, hosts []string) error {
	if strings.TrimSpace(token) != "" {
		return nil
	}
	if strings.TrimSpace(insecure) != "1" {
		return errors.New("AGENT_TOKEN is not set; refusing to start (set AGENT_TOKEN, or GUARDIAN_INSECURE_NO_AUTH=1 for a loopback-only dev agent)")
	}
	if len(hosts) == 0 {
		return errors.New("GUARDIAN_INSECURE_NO_AUTH=1 needs at least one loopback listen address")
	}
	for _, h := range hosts {
		if !isLoopbackHost(h) {
			return fmt.Errorf("GUARDIAN_INSECURE_NO_AUTH=1 is only allowed on loopback, but AGENT_HOST includes %q; set AGENT_TOKEN", h)
		}
	}
	return nil
}

// hostAllowed rejects DNS-rebinding requests: the Host header must be an IP literal,
// localhost, or a name the operator allowed. An empty Host (HTTP/1.0) is rejected.
func (c securityConfig) hostAllowed(hostport string) bool {
	host := hostport
	if h, _, err := net.SplitHostPort(hostport); err == nil {
		host = h
	}
	if _, ok := parseIPLiteral(host); ok {
		return true
	}
	host = normalizeHostName(host)
	if host == "" {
		return false
	}
	return host == "localhost" || c.allowedHosts[host]
}

// originAllowed reports whether a browser Origin may call the API: the agent's own origin
// (same host and port as the request) or an allowlisted cross-origin client. "null" and
// non-http(s) origins are only accepted when allowlisted (and "null" never is).
func (c securityConfig) originAllowed(origin, requestHost string) bool {
	if c.corsOrigins[strings.ToLower(origin)] {
		return true
	}
	u, err := url.Parse(origin)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" ||
		u.User != nil || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" {
		return false
	}
	return strings.EqualFold(u.Host, requestHost)
}

// crossSiteWithoutOrigin reports whether a browser marked the request as coming from another
// site even though it carries no Origin (e.g. <img src>, <script src>, a link or form GET).
// Such requests are never legitimate API calls; curl and the CLI send no Sec-Fetch-Site.
func crossSiteWithoutOrigin(r *http.Request) bool {
	switch strings.ToLower(r.Header.Get("Sec-Fetch-Site")) {
	case "cross-site", "same-site":
		return true
	}
	return false
}

// hasJSONBody reports whether a request is acceptable with respect to Content-Type.
// Body-less GET/HEAD always pass. For other methods, a request with a body or with any
// declared Content-Type must be application/json: a cross-site HTML form can only send
// form-encoded, multipart or text/plain bodies.
func hasJSONBody(r *http.Request) bool {
	ct := r.Header.Get("Content-Type")
	if r.ContentLength == 0 && (ct == "" || r.Method == http.MethodGet || r.Method == http.MethodHead) {
		return true
	}
	mt, _, err := mime.ParseMediaType(ct)
	return err == nil && mt == "application/json"
}

func isAPIPath(p string) bool {
	return p == "/api" || strings.HasPrefix(p, "/api/")
}

// @MX:ANCHOR: [AUTO] Request policy in front of withAuth for every /api route: Host, Origin, CORS and Content-Type.
// @MX:REASON: Blocks DNS rebinding and cross-site requests to /api/exec and the fleet endpoints, which run shell commands.
// withSecurity guards /api: a Host that is not an IP literal, localhost or allowlisted is 421,
// a present Origin that is neither same-origin nor allowlisted is 403 (as is a browser
// request flagged cross-site without an Origin), and a non-JSON body is 415. CORS headers are
// only sent to allowlisted origins; OPTIONS preflights are answered here and never reach the mux.
func withSecurity(next http.Handler, cfg securityConfig) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !isAPIPath(r.URL.Path) {
			next.ServeHTTP(w, r)
			return
		}
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if !cfg.hostAllowed(r.Host) {
			jsonResponse(w, http.StatusMisdirectedRequest, map[string]string{"error": "host not allowed (add it to AGENT_ALLOWED_HOSTS)"})
			return
		}
		origin := r.Header.Get("Origin")
		w.Header().Add("Vary", "Origin")
		if origin != "" {
			if !cfg.originAllowed(origin, r.Host) {
				jsonResponse(w, http.StatusForbidden, map[string]string{"error": "origin not allowed (add it to AGENT_CORS_ORIGINS)"})
				return
			}
			if cfg.corsOrigins[strings.ToLower(origin)] {
				w.Header().Set("Access-Control-Allow-Origin", origin)
			}
		} else if crossSiteWithoutOrigin(r) {
			jsonResponse(w, http.StatusForbidden, map[string]string{"error": "cross-site request not allowed"})
			return
		}
		if r.Method == http.MethodOptions {
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
			w.Header().Set("Access-Control-Max-Age", "600")
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if !hasJSONBody(r) {
			jsonResponse(w, http.StatusUnsupportedMediaType, map[string]string{"error": "Content-Type must be application/json"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

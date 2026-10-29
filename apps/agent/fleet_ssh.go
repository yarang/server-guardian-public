package main

import (
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Fleet SSH audit: runs a fixed read-only probe on every node over the fleet key and
// evaluates the captured sshd config with the same parser and rules as the hub's own audit.
// Design: docs/design/fleet-ssh-audit.md.

const (
	fleetSSHMarker      = "@@GSA"
	fleetSSHTimeoutSec  = 20
	fleetSSHConcurrency = 8
	fleetSSHCacheTTL    = 60 * time.Second
	fleetSSHCacheMax    = 64
	fleetSSHMainConfig  = "/etc/ssh/sshd_config"
	fleetSSHDropInDir   = "/etc/ssh/sshd_config.d/"
	fleetSSHSourceSudo  = "sshd -T (sudo)"
	fleetSSHSourceRoot  = "sshd -T"
	fleetSSHSourceFiles = "sshd_config (fallback)"
)

// fleetSSHProbe is the only script the audit runs. It is a constant: no request value is
// ever interpolated into it (targets only pick the ssh destination from servers.json). It is
// wrapped in sh -c '...' so the node's login shell does not matter, which is why the body
// must never contain a single quote (TestFleetSSHProbeIsFixedAndQuoteFree checks this).
const fleetSSHProbe = `sh -c '
export LC_ALL=C
PATH="$PATH:/usr/sbin:/sbin:/usr/local/sbin"
m="@@GSA"
{
echo "$m BEGIN 1"
echo "$m USER"
id -un
got=0
for via in sudo direct; do
  if [ "$via" = sudo ]; then out=$(sudo -n sshd -T 2>&1 </dev/null); rc=$?
  else out=$(sshd -T 2>&1 </dev/null); rc=$?; fi
  if [ "$rc" -eq 0 ]; then
    echo "$m EFFECTIVE $via"
    printf "%s\n" "$out" | head -c 65536
    got=1
    break
  fi
  echo "$m EFFECTIVE_ERR $via $rc"
  printf "%s\n" "$out" | head -c 2048
done
if [ "$got" -eq 0 ]; then
  n=0
  for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*; do
    [ -f "$f" ] || continue
    n=$((n+1)); [ "$n" -le 32 ] || break
    if [ -r "$f" ]; then echo "$m FILE $f"; head -c 65536 "$f"; echo
    else echo "$m UNREADABLE $f"; fi
  done
fi
echo "$m KEYS"
if [ -r "$HOME/.ssh/authorized_keys" ]; then head -c 65536 "$HOME/.ssh/authorized_keys"; echo; fi
echo "$m END"
} 2>/dev/null | head -c 262144
'`

// fleetSSHExec runs the probe on one node; tests replace it so no ssh ever starts.
var fleetSSHExec = executeSSH

// FleetSSHNode is one node's audit. Config and Report are absent when nothing could be
// evaluated (unreachable, error): a node is never scored on defaults.
type FleetSSHNode struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Status string `json:"status"` // "ok", "partial", "unreachable", "error"
	// Reachability is the shared ssh classification (classifySSH): ok, auth_failed,
	// host_key_changed, unreachable or command_failed. Status keeps its audit meaning.
	Reachability string            `json:"reachability"`
	Source       string            `json:"source,omitempty"`
	LoginUser    string            `json:"login_user,omitempty"`
	DurationMs   int64             `json:"duration_ms"`
	Config       *SSHConfigSummary `json:"config,omitempty"`
	Report       *SecurityReport   `json:"report,omitempty"`
	Keys         []AuthorizedKey   `json:"keys"`
	Notes        []string          `json:"notes"`
	Error        string            `json:"error,omitempty"`
}

// FleetSSHRuleTally counts one rule's statuses across audited nodes.
type FleetSSHRuleTally struct {
	Fail int `json:"fail"`
	Warn int `json:"warn"`
	Pass int `json:"pass"`
	Info int `json:"info"`
}

// FleetSSHRuleInfo describes a rule column (evaluation order).
type FleetSSHRuleInfo struct {
	RuleID string `json:"rule_id"`
	Title  string `json:"title"`
}

// FleetSSHSummary aggregates the nodes; per-group aggregation is left to the UI and CLI,
// which own group membership.
type FleetSSHSummary struct {
	Nodes        int                           `json:"nodes"`
	Audited      int                           `json:"audited"`
	Unreachable  int                           `json:"unreachable"`
	Errors       int                           `json:"errors"`
	AvgScore     *float64                      `json:"avg_score"`
	FailingNodes []string                      `json:"failing_nodes"`
	Rules        []FleetSSHRuleInfo            `json:"rules"`
	ByRule       map[string]*FleetSSHRuleTally `json:"by_rule"`
}

// FleetSSHReport is the GET /api/fleet/ssh response.
type FleetSSHReport struct {
	GeneratedAt time.Time       `json:"generated_at"`
	DurationMs  int64           `json:"duration_ms"`
	Cached      bool            `json:"cached"`
	Nodes       []FleetSSHNode  `json:"nodes"`
	Summary     FleetSSHSummary `json:"summary"`
}

// fleetSSHProbeOutput is the probe's stdout split at its section markers.
type fleetSSHProbeOutput struct {
	Begin, End    bool
	User          string
	Effective     string
	EffectiveVia  string // "sudo" or "direct"; empty when sshd -T never succeeded
	EffectiveErrs []string
	Files         map[string]string
	Unreadable    []string
	Keys          string
	HasKeys       bool
}

// splitFleetSSHProbe parses probe output. Lines starting with the marker open a section;
// unknown markers are ignored and a repeated section keeps its first body, so a node can
// only distort its own result.
func splitFleetSSHProbe(stdout string) fleetSSHProbeOutput {
	out := fleetSSHProbeOutput{Files: map[string]string{}}
	var section, arg string
	var body []string
	seen := map[string]bool{}
	flush := func() {
		text := strings.Join(body, "\n")
		key := section + " " + arg
		body = nil
		if section == "" || seen[key] {
			return
		}
		seen[key] = true
		switch section {
		case "USER":
			out.User = strings.TrimSpace(text)
		case "EFFECTIVE":
			if out.EffectiveVia == "" {
				out.Effective, out.EffectiveVia = text, arg
			}
		case "EFFECTIVE_ERR":
			via, rc, _ := strings.Cut(arg, " ")
			msg := strings.TrimSpace(text)
			if msg == "" {
				msg = "exit " + rc
			}
			out.EffectiveErrs = append(out.EffectiveErrs, via+": "+msg)
		case "FILE":
			if arg != "" {
				out.Files[arg] = text
			}
		case "UNREADABLE":
			if arg != "" {
				out.Unreadable = append(out.Unreadable, arg)
			}
		case "KEYS":
			out.Keys, out.HasKeys = text, true
		}
	}
	for _, line := range strings.Split(stdout, "\n") {
		rest, ok := strings.CutPrefix(line, fleetSSHMarker+" ")
		if !ok {
			if section != "" {
				body = append(body, line)
			}
			continue
		}
		flush()
		name, a, _ := strings.Cut(strings.TrimSpace(rest), " ")
		section, arg = name, strings.TrimSpace(a)
		switch name {
		case "BEGIN":
			out.Begin = true
			section = ""
		case "END":
			out.End = true
			section = ""
		}
	}
	flush()
	return out
}

// parseAuthorizedKeysText parses authorized_keys content. The key body is dropped: the
// fingerprint identifies the key and keeps the fleet response small.
func parseAuthorizedKeysText(text string) []AuthorizedKey {
	keys := []AuthorizedKey{}
	for _, raw := range strings.Split(text, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, ok := parseAuthorizedKeyLine(line)
		if !ok {
			continue
		}
		k.ID = "key-" + strconv.Itoa(len(keys)+1)
		k.PublicKey = ""
		keys = append(keys, k)
	}
	return keys
}

// lastLine returns the last non-empty line of s (ssh puts the useful error there).
func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if l := strings.TrimSpace(lines[i]); l != "" {
			return l
		}
	}
	return ""
}

// auditFromProbe turns one node's probe result into its audit entry (status table in
// docs/design/fleet-ssh-audit.md section 4).
func auditFromProbe(srv FleetNode, exitCode int, stdout, stderr string, durMs int64) FleetSSHNode {
	node := FleetSSHNode{ID: srv.ID, Name: srv.Name, DurationMs: durMs, Keys: []AuthorizedKey{}, Notes: []string{}}
	if node.Name == "" {
		node.Name = srv.ID
	}
	p := splitFleetSSHProbe(stdout)
	node.Reachability = reachOK
	if !p.Begin {
		node.Reachability = classifySSH(exitCode, stderr)
		if node.Reachability == reachOK {
			node.Reachability = reachCommandFailed // exit 0 without the probe markers
		}
		msg := lastLine(stderr)
		// 255 is ssh's own failure (connect, auth, host key); -1 is the timeout kill.
		if exitCode == 255 || exitCode < 0 {
			node.Status = "unreachable"
			if msg == "" {
				msg = "ssh failed (exit " + strconv.Itoa(exitCode) + ")"
				if exitCode < 0 {
					msg = "ssh timed out after " + strconv.Itoa(fleetSSHTimeoutSec) + "s"
				}
			}
		} else {
			node.Status = "error"
			if msg == "" {
				msg = "probe produced no output (exit " + strconv.Itoa(exitCode) + ")"
			}
		}
		node.Error = msg
		return node
	}
	node.LoginUser = p.User
	node.Keys = parseAuthorizedKeysText(p.Keys)

	var cfg SSHConfigSummary
	switch {
	case p.EffectiveVia != "":
		cfg = SummaryFromEffectiveConfig(p.Effective, fleetSSHMainConfig)
		node.Status = "ok"
		node.Source = fleetSSHSourceRoot
		if p.EffectiveVia == "sudo" {
			node.Source = fleetSSHSourceSudo
		}
	case hasFile(p.Files, fleetSSHMainConfig):
		var includes []string
		var err error
		cfg, includes, err = ParseSSHDConfigText(fleetSSHMainConfig, p.Files)
		if err != nil {
			node.Status = "error"
			node.Error = "sshd_config could not be parsed: " + err.Error()
			return node
		}
		node.Status = "partial"
		node.Source = fleetSSHSourceFiles
		cfg.Source = fleetSSHSourceFiles
		for _, e := range p.EffectiveErrs {
			if _, msg, _ := strings.Cut(e, ": "); isSSHDConfigError(msg) {
				cfg.SyntaxChecked, cfg.IsValid, cfg.SyntaxMessage = true, false, msg
				break
			}
		}
		node.Notes = append(node.Notes, "sshd -T could not run (needs root; sudo -n refused), so the config files were read instead: values set by unreadable includes and changed sshd defaults may be missing.")
		for _, e := range p.EffectiveErrs {
			node.Notes = append(node.Notes, "sshd -T via "+e)
		}
		for _, inc := range includes {
			if !strings.HasPrefix(inc, fleetSSHDropInDir) {
				node.Notes = append(node.Notes, "Include "+inc+" is outside "+fleetSSHDropInDir+" and was not captured.")
			}
		}
	default:
		node.Status = "error"
		node.Error = "no readable sshd configuration: sshd -T failed and " + fleetSSHMainConfig + " is not readable"
		if len(p.EffectiveErrs) > 0 {
			node.Error += " (" + strings.Join(p.EffectiveErrs, "; ") + ")"
		}
		return node
	}
	for _, u := range p.Unreadable {
		node.Notes = append(node.Notes, "Not readable without root: "+u)
	}
	if !p.End {
		node.Status = "partial"
		node.Notes = append(node.Notes, "Probe output incomplete (truncated or interrupted); later sections such as authorized_keys may be missing.")
	}
	if !p.HasKeys {
		node.Notes = append(node.Notes, "authorized_keys section missing from the probe output.")
	}
	report := EvaluateSecurity(cfg)
	node.Config = &cfg
	node.Report = &report
	return node
}

func hasFile(files map[string]string, path string) bool {
	_, ok := files[path]
	return ok
}

// fleetSSHRuleCatalog lists the rules in evaluation order (the heatmap's columns).
func fleetSSHRuleCatalog() []FleetSSHRuleInfo {
	var out []FleetSSHRuleInfo
	for _, r := range EvaluateSecurity(defaultSSHConfigSummary("")).Rules {
		out = append(out, FleetSSHRuleInfo{RuleID: r.RuleID, Title: r.Title})
	}
	return out
}

// summarizeFleetSSH aggregates node audits. Only FAIL makes a node failing, as on the
// hub's own audit; nodes without a report are left out of scores and rule counts.
func summarizeFleetSSH(nodes []FleetSSHNode) FleetSSHSummary {
	s := FleetSSHSummary{
		Nodes:        len(nodes),
		FailingNodes: []string{},
		Rules:        fleetSSHRuleCatalog(),
		ByRule:       map[string]*FleetSSHRuleTally{},
	}
	for _, r := range s.Rules {
		s.ByRule[r.RuleID] = &FleetSSHRuleTally{}
	}
	total := 0
	for _, n := range nodes {
		switch n.Status {
		case "unreachable":
			s.Unreachable++
		case "error":
			s.Errors++
		}
		if n.Report == nil {
			continue
		}
		s.Audited++
		total += n.Report.Score
		if n.Report.FailedCount > 0 {
			s.FailingNodes = append(s.FailingNodes, n.ID)
		}
		for _, r := range n.Report.Rules {
			t := s.ByRule[r.RuleID]
			if t == nil {
				t = &FleetSSHRuleTally{}
				s.ByRule[r.RuleID] = t
			}
			switch r.Status {
			case "FAIL":
				t.Fail++
			case "WARN":
				t.Warn++
			case "PASS":
				t.Pass++
			default:
				t.Info++
			}
		}
	}
	if s.Audited > 0 {
		avg := math.Round(float64(total)/float64(s.Audited)*10) / 10
		s.AvgScore = &avg
	}
	return s
}

// @MX:WARN: [AUTO] Fans out one ssh per node through goroutines without a context; the semaphore bounds them.
// @MX:REASON: Each executeSSH call has its own kill timer (fleetSSHTimeoutSec), so the fan-out always ends.
// AuditFleetSSH probes nodes in parallel (at most fleetSSHConcurrency at a time) and
// returns their audits in servers.json order.
func AuditFleetSSH(fleet []FleetNode) FleetSSHReport {
	start := time.Now()
	nodes := make([]FleetSSHNode, len(fleet))
	sem := make(chan struct{}, fleetSSHConcurrency)
	var wg sync.WaitGroup
	for i, s := range fleet {
		wg.Add(1)
		go func(i int, srv FleetNode) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			code, stdout, stderr, dur := fleetSSHExec(srv.ID, fleetSSHProbe, fleetSSHTimeoutSec)
			nodes[i] = auditFromProbe(srv, code, stdout, stderr, dur)
		}(i, s)
	}
	wg.Wait()
	return FleetSSHReport{
		GeneratedAt: time.Now().UTC(),
		DurationMs:  time.Since(start).Milliseconds(),
		Nodes:       nodes,
		Summary:     summarizeFleetSSH(nodes),
	}
}

// fleetSSHCache keeps recent reports per target set for a short TTL and lets concurrent
// identical requests share one run (single-flight), so dashboards and CLI calls do not
// multiply the SSH fan-out.
type fleetSSHCache struct {
	mu       sync.Mutex
	ttl      time.Duration
	now      func() time.Time
	entries  map[string]fleetSSHEntry
	inflight map[string]*fleetSSHCall
}

type fleetSSHEntry struct {
	report FleetSSHReport
	at     time.Time
}

type fleetSSHCall struct {
	done   chan struct{}
	report FleetSSHReport
}

func newFleetSSHCache(ttl time.Duration) *fleetSSHCache {
	return &fleetSSHCache{ttl: ttl, now: time.Now, entries: map[string]fleetSSHEntry{}, inflight: map[string]*fleetSSHCall{}}
}

var fleetSSHAuditCache = newFleetSSHCache(fleetSSHCacheTTL)

// fleetSSHCacheKey is the sorted id set, so ?targets=b,a and a,b share an entry.
func fleetSSHCacheKey(fleet []FleetNode) string {
	ids := make([]string, 0, len(fleet))
	for _, s := range fleet {
		ids = append(ids, s.ID)
	}
	sort.Strings(ids)
	return strings.Join(ids, ",")
}

// get returns a fresh cached report (cached=true) or runs one. refresh skips the cache but
// still joins a run already in flight, whose result is just as new.
func (c *fleetSSHCache) get(key string, refresh bool, run func() FleetSSHReport) (FleetSSHReport, bool) {
	c.mu.Lock()
	if e, ok := c.entries[key]; ok && !refresh && c.now().Sub(e.at) < c.ttl {
		c.mu.Unlock()
		return e.report, true
	}
	if call, ok := c.inflight[key]; ok {
		c.mu.Unlock()
		<-call.done
		return call.report, false
	}
	call := &fleetSSHCall{done: make(chan struct{})}
	c.inflight[key] = call
	c.mu.Unlock()

	defer func() {
		c.mu.Lock()
		delete(c.inflight, key)
		c.mu.Unlock()
		close(call.done)
	}()
	call.report = run()

	c.mu.Lock()
	now := c.now()
	for k, e := range c.entries {
		if now.Sub(e.at) >= c.ttl {
			delete(c.entries, k)
		}
	}
	if len(c.entries) >= fleetSSHCacheMax {
		oldest := ""
		for k, e := range c.entries {
			if oldest == "" || e.at.Before(c.entries[oldest].at) {
				oldest = k
			}
		}
		delete(c.entries, oldest)
	}
	c.entries[key] = fleetSSHEntry{report: call.report, at: now}
	c.mu.Unlock()
	return call.report, false
}

// @MX:NOTE: [AUTO] GET /api/fleet/ssh?targets=a,b[&refresh=1]. Targets are checked against servers.json
// before any ssh starts (unknown -> 400); the probe itself is a constant (fleetSSHProbe).
func handleFleetSSH(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET required"})
		return
	}
	fleet, err := fleetSubset(targetsFromQuery(r))
	if err != nil {
		jsonResponse(w, fleetErrorStatus(err), map[string]string{"error": err.Error()})
		return
	}
	refresh := r.URL.Query().Get("refresh") == "1"
	report, cached := fleetSSHAuditCache.get(fleetSSHCacheKey(fleet), refresh, func() FleetSSHReport {
		return AuditFleetSSH(fleet)
	})
	report.Cached = cached
	jsonResponse(w, http.StatusOK, report)
}

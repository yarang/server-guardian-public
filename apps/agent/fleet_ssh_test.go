package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const testKeyBlob = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

// probeOut assembles probe output the way fleetSSHProbe prints it.
func probeOut(sections ...string) string {
	return "@@GSA BEGIN 1\n" + strings.Join(sections, "\n") + "\n@@GSA END\n"
}

const sshdTSample = `port 2222
addressfamily any
listenaddress 0.0.0.0:2222
permitrootlogin without-password
pubkeyauthentication yes
passwordauthentication no
maxauthtries 3
x11forwarding no
allowtcpforwarding no
ciphers chacha20-poly1305@openssh.com
authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2`

// stubFleetSSH replaces the executor; outputs maps node id to (code, stdout, stderr).
type fakeRun struct {
	code           int
	stdout, stderr string
}

func stubFleetSSH(t *testing.T, outputs map[string]fakeRun) *int32 {
	t.Helper()
	var calls int32
	orig := fleetSSHExec
	fleetSSHExec = func(host, cmd string, timeout int) (int, string, string, int64) {
		atomic.AddInt32(&calls, 1)
		if cmd != fleetSSHProbe {
			t.Errorf("executor got a command other than the fixed probe: %q", cmd)
		}
		r, ok := outputs[host]
		if !ok {
			return 255, "", "ssh: connect to host " + host + " port 22: Connection refused", 5
		}
		return r.code, r.stdout, r.stderr, 7
	}
	t.Cleanup(func() { fleetSSHExec = orig })
	return &calls
}

func resetFleetSSHCache(t *testing.T) {
	t.Helper()
	orig := fleetSSHAuditCache
	fleetSSHAuditCache = newFleetSSHCache(fleetSSHCacheTTL)
	t.Cleanup(func() { fleetSSHAuditCache = orig })
}

func TestFleetSSHProbeIsFixedAndQuoteFree(t *testing.T) {
	body := strings.TrimSuffix(strings.TrimPrefix(fleetSSHProbe, "sh -c '"), "'")
	if body == fleetSSHProbe || strings.Contains(body, "'") {
		t.Fatal("probe must be sh -c '<body>' with no single quote inside the body")
	}
	for _, want := range []string{"sudo -n sshd -T", "sshd -T 2>&1", "/etc/ssh/sshd_config.d/*; do", ".ssh/authorized_keys", "head -c 262144", "$m END"} {
		if !strings.Contains(fleetSSHProbe, want) {
			t.Errorf("probe lacks %q", want)
		}
	}
	for _, banned := range []string{"rm ", "tee ", ">/etc", "systemctl"} {
		if strings.Contains(fleetSSHProbe, banned) {
			t.Errorf("probe contains %q", banned)
		}
	}
}

func TestSplitFleetSSHProbe(t *testing.T) {
	out := probeOut(
		"@@GSA USER", "ubuntu",
		"@@GSA EFFECTIVE_ERR sudo 1", "sudo: a password is required",
		"@@GSA EFFECTIVE_ERR direct 255", "sshd: no hostkeys available -- exiting.",
		"@@GSA FILE /etc/ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf", "PasswordAuthentication yes",
		"@@GSA FILE /etc/ssh/sshd_config.d/50-x.conf", "PasswordAuthentication no",
		"@@GSA UNREADABLE /etc/ssh/sshd_config.d/60-secret.conf",
		"@@GSA BOGUS whatever", "ignored",
		"@@GSA FILE /etc/ssh/sshd_config", "PermitRootLogin yes", // repeated: first one wins
		"@@GSA KEYS", "ssh-ed25519 "+testKeyBlob+" me@laptop",
	)
	p := splitFleetSSHProbe(out)
	if !p.Begin || !p.End || p.User != "ubuntu" || p.EffectiveVia != "" {
		t.Fatalf("begin %v end %v user %q via %q", p.Begin, p.End, p.User, p.EffectiveVia)
	}
	if len(p.EffectiveErrs) != 2 || !strings.HasPrefix(p.EffectiveErrs[0], "sudo: sudo: a password") {
		t.Errorf("errs %q", p.EffectiveErrs)
	}
	if len(p.Files) != 2 || !strings.Contains(p.Files["/etc/ssh/sshd_config"], "PasswordAuthentication yes") ||
		strings.Contains(p.Files["/etc/ssh/sshd_config"], "PermitRootLogin") {
		t.Errorf("files %q", p.Files)
	}
	if len(p.Unreadable) != 1 || !p.HasKeys || !strings.Contains(p.Keys, "me@laptop") {
		t.Errorf("unreadable %v keys %q", p.Unreadable, p.Keys)
	}
	if strings.Contains(p.Keys, "ignored") || strings.Contains(p.Files["/etc/ssh/sshd_config.d/50-x.conf"], "ignored") {
		t.Error("unknown marker body leaked into another section")
	}
}

func TestSummaryFromEffectiveConfigText(t *testing.T) {
	c := SummaryFromEffectiveConfig(sshdTSample, "/etc/ssh/sshd_config")
	if c.Port != 2222 || c.PermitRootLogin != "prohibit-password" || c.PasswordAuth != "no" || c.MaxAuthTries != 3 ||
		c.AllowTcpForwarding != "no" || !c.SyntaxChecked || !c.IsValid || c.Source != "sshd -T" {
		t.Fatalf("%+v", c)
	}
}

// The fallback reads captured files like sshd reads disk: relative Include under /etc/ssh,
// first value wins, Match skipped (also an Include inside Match), and a Match in an
// included file does not leak back into the including file.
func TestParseSSHDConfigTextFallback(t *testing.T) {
	files := map[string]string{
		"/etc/ssh/sshd_config": "" +
			"# defaults\n" +
			"Include sshd_config.d/*.conf\n" +
			"Port=2200\n" +
			"MAXAUTHTRIES 5\n" +
			"PasswordAuthentication yes\n" +
			"Match Group admins\n" +
			"  Include /etc/ssh/match-only.conf\n" +
			"  PermitRootLogin yes\n",
		"/etc/ssh/sshd_config.d/10-a.conf": "PasswordAuthentication no\nMatch User backup\n  X11Forwarding yes\n",
		"/etc/ssh/sshd_config.d/20-b.conf": "X11Forwarding no\nPermitRootLogin no\nPasswordAuthentication yes\n",
		"/etc/ssh/match-only.conf":         "PubkeyAuthentication no\n",
		"/etc/ssh/sshd_config.d/notes.txt": "Port 1\n",
	}
	c, includes, err := ParseSSHDConfigText("/etc/ssh/sshd_config", files)
	if err != nil {
		t.Fatal(err)
	}
	if c.PasswordAuth != "no" || c.PermitRootLogin != "no" || c.Port != 2200 || c.MaxAuthTries != 5 {
		t.Errorf("values %+v", c)
	}
	if c.X11Forwarding != "no" || c.PubkeyAuth != "yes" {
		t.Errorf("Match leaked: X11 %q Pubkey %q", c.X11Forwarding, c.PubkeyAuth)
	}
	if c.SyntaxChecked || len(includes) != 1 || includes[0] != "/etc/ssh/sshd_config.d/*.conf" {
		t.Errorf("checked %v includes %v", c.SyntaxChecked, includes)
	}
	if _, _, err := ParseSSHDConfigText("/etc/ssh/sshd_config", map[string]string{}); err == nil {
		t.Error("missing main config should be an error")
	}
}

func TestAuditFromProbeStatuses(t *testing.T) {
	srv := FleetNode{ID: "n1", Name: "node-1"}
	key := "from=\"100.64.0.7\",no-agent-forwarding ssh-ed25519 " + testKeyBlob + " guardian-hub-fleet@hub"

	ok := auditFromProbe(srv, 0, probeOut("@@GSA USER", "ubuntu", "@@GSA EFFECTIVE sudo", sshdTSample, "@@GSA KEYS", key, "# comment", "garbage"), "", 10)
	if ok.Status != "ok" || ok.Source != fleetSSHSourceSudo || ok.Report == nil || ok.Config == nil || ok.LoginUser != "ubuntu" {
		t.Fatalf("ok node: %+v", ok)
	}
	if len(ok.Keys) != 1 || ok.Keys[0].PublicKey != "" || ok.Keys[0].Fingerprint != "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" ||
		ok.Keys[0].Options != `from="100.64.0.7",no-agent-forwarding` {
		t.Errorf("keys %+v", ok.Keys)
	}

	partial := auditFromProbe(srv, 0, probeOut(
		"@@GSA USER", "ubuntu",
		"@@GSA EFFECTIVE_ERR sudo 1", "sudo: a password is required",
		"@@GSA EFFECTIVE_ERR direct 1", "sshd: no hostkeys available -- exiting.",
		"@@GSA FILE /etc/ssh/sshd_config", "Include /etc/other/*.conf", "PermitRootLogin no",
		"@@GSA UNREADABLE /etc/ssh/sshd_config.d/50-cloud-init.conf",
		"@@GSA KEYS"), "", 10)
	if partial.Status != "partial" || partial.Source != fleetSSHSourceFiles || partial.Config.Source != fleetSSHSourceFiles ||
		partial.Config.SyntaxChecked || partial.Report == nil || partial.Config.PermitRootLogin != "no" {
		t.Fatalf("partial node: %+v %+v", partial, partial.Config)
	}
	notes := strings.Join(partial.Notes, "\n")
	for _, want := range []string{"sshd -T could not run", "/etc/other/*.conf", "50-cloud-init.conf", "password is required"} {
		if !strings.Contains(notes, want) {
			t.Errorf("notes lack %q: %s", want, notes)
		}
	}

	bad := auditFromProbe(srv, 0, probeOut(
		"@@GSA EFFECTIVE_ERR sudo 255", "/etc/ssh/sshd_config line 3: Bad configuration option: Bogus",
		"@@GSA EFFECTIVE_ERR direct 255", "sshd: no hostkeys available -- exiting.",
		"@@GSA FILE /etc/ssh/sshd_config", "Bogus yes", "@@GSA KEYS"), "", 10)
	if bad.Status != "partial" || !bad.Config.SyntaxChecked || bad.Config.IsValid || !strings.Contains(bad.Config.SyntaxMessage, "Bogus") {
		t.Errorf("syntax error node: %+v", bad.Config)
	}

	truncated := auditFromProbe(srv, 0, "@@GSA BEGIN 1\n@@GSA EFFECTIVE direct\n"+sshdTSample, "", 10)
	if truncated.Status != "partial" || truncated.Source != fleetSSHSourceRoot || truncated.Report == nil {
		t.Errorf("truncated node: %+v", truncated)
	}

	unreachable := auditFromProbe(srv, 255, "", "Warning: x\nssh: connect to host n1 port 22: No route to host\n", 4000)
	if unreachable.Status != "unreachable" || unreachable.Report != nil || !strings.Contains(unreachable.Error, "No route") {
		t.Errorf("unreachable node: %+v", unreachable)
	}
	if timedOut := auditFromProbe(srv, -1, "", "", 20000); timedOut.Status != "unreachable" || !strings.Contains(timedOut.Error, "timed out") {
		t.Errorf("timed out node: %+v", timedOut)
	}
	if broken := auditFromProbe(srv, 127, "", "sh: not found", 5); broken.Status != "error" || broken.Report != nil {
		t.Errorf("no-probe node: %+v", broken)
	}
	none := auditFromProbe(srv, 0, probeOut("@@GSA EFFECTIVE_ERR sudo 1", "sudo: a password is required",
		"@@GSA UNREADABLE /etc/ssh/sshd_config", "@@GSA KEYS"), "", 5)
	if none.Status != "error" || none.Config != nil || !strings.Contains(none.Error, "no readable sshd configuration") {
		t.Errorf("unreadable node: %+v", none)
	}
}

func TestSummarizeFleetSSH(t *testing.T) {
	good := EvaluateSecurity(SSHConfigSummary{Port: 2222, PermitRootLogin: "no", PasswordAuth: "no", PubkeyAuth: "yes", MaxAuthTries: 3})
	weak := EvaluateSecurity(SSHConfigSummary{Port: 22, PermitRootLogin: "prohibit-password", PasswordAuth: "yes", PubkeyAuth: "yes", MaxAuthTries: 6})
	warnOnly := EvaluateSecurity(SSHConfigSummary{Port: 22, PermitRootLogin: "prohibit-password", PasswordAuth: "no", PubkeyAuth: "yes", MaxAuthTries: 3})
	s := summarizeFleetSSH([]FleetSSHNode{
		{ID: "a", Status: "ok", Report: &good},
		{ID: "b", Status: "partial", Report: &weak},
		{ID: "c", Status: "ok", Report: &warnOnly},
		{ID: "d", Status: "unreachable"},
		{ID: "e", Status: "error"},
	})
	if s.Nodes != 5 || s.Audited != 3 || s.Unreachable != 1 || s.Errors != 1 {
		t.Fatalf("%+v", s)
	}
	wantAvg := float64(good.Score+weak.Score+warnOnly.Score) / 3
	if s.AvgScore == nil || *s.AvgScore < wantAvg-0.05 || *s.AvgScore > wantAvg+0.05 {
		t.Errorf("avg %v want %.1f", s.AvgScore, wantAvg)
	}
	if len(s.FailingNodes) != 1 || s.FailingNodes[0] != "b" {
		t.Errorf("failing %v (WARN/INFO alone must not count)", s.FailingNodes)
	}
	if len(s.Rules) != 5 || s.Rules[0].RuleID != "SEC-SSH-001" {
		t.Errorf("rules %+v", s.Rules)
	}
	if r := s.ByRule["SEC-SSH-001"]; r.Pass != 1 || r.Warn != 2 || r.Fail != 0 {
		t.Errorf("SEC-SSH-001 %+v", r)
	}
	if r := s.ByRule["SEC-SSH-002"]; r.Fail != 1 || r.Pass != 2 {
		t.Errorf("SEC-SSH-002 %+v", r)
	}
	if r := s.ByRule["SEC-SSH-005"]; r.Info != 2 || r.Pass != 1 {
		t.Errorf("SEC-SSH-005 %+v", r)
	}
	if empty := summarizeFleetSSH(nil); empty.AvgScore != nil || empty.FailingNodes == nil {
		t.Errorf("empty summary %+v", empty)
	}
}

func TestHandleFleetSSHValidatesTargetsBeforeSSH(t *testing.T) {
	writeServers(t, `[{"id":"a","name":"n-a"},{"id":"b","name":"n-b"}]`)
	resetFleetSSHCache(t)
	calls := stubFleetSSH(t, nil)

	for _, c := range []struct {
		method, url string
		code        int
	}{
		{http.MethodGet, "/api/fleet/ssh?targets=a,zz", http.StatusBadRequest},
		{http.MethodGet, "/api/fleet/ssh?targets=-oProxyCommand=x", http.StatusBadRequest},
		{http.MethodPost, "/api/fleet/ssh", http.StatusMethodNotAllowed},
	} {
		rec := httptest.NewRecorder()
		handleFleetSSH(rec, httptest.NewRequest(c.method, c.url, nil))
		if rec.Code != c.code {
			t.Errorf("%s %s = %d %s", c.method, c.url, rec.Code, rec.Body.String())
		}
	}
	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("ssh ran %d times for rejected requests", n)
	}
}

func TestHandleFleetSSHAuditsAndCaches(t *testing.T) {
	writeServers(t, `[{"id":"a","name":"n-a"},{"id":"b","name":"n-b"},{"id":"c","name":"n-c"}]`)
	resetFleetSSHCache(t)
	calls := stubFleetSSH(t, map[string]fakeRun{
		"a": {0, probeOut("@@GSA USER", "ubuntu", "@@GSA EFFECTIVE sudo", sshdTSample, "@@GSA KEYS", "ssh-ed25519 "+testKeyBlob+" me"), ""},
		"c": {0, probeOut("@@GSA EFFECTIVE_ERR sudo 1", "sudo: a password is required", "@@GSA FILE /etc/ssh/sshd_config", "PasswordAuthentication yes", "@@GSA KEYS"), ""},
	})
	get := func(url string) FleetSSHReport {
		t.Helper()
		rec := httptest.NewRecorder()
		handleFleetSSH(rec, httptest.NewRequest(http.MethodGet, url, nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("%s = %d %s", url, rec.Code, rec.Body.String())
		}
		var r FleetSSHReport
		if err := json.Unmarshal(rec.Body.Bytes(), &r); err != nil {
			t.Fatal(err)
		}
		return r
	}

	r := get("/api/fleet/ssh")
	if r.Cached || len(r.Nodes) != 3 || r.Nodes[0].ID != "a" || r.Nodes[1].ID != "b" || r.Nodes[2].ID != "c" {
		t.Fatalf("order/cached: %+v", r)
	}
	if r.Nodes[0].Status != "ok" || r.Nodes[1].Status != "unreachable" || r.Nodes[2].Status != "partial" {
		t.Errorf("statuses %s %s %s", r.Nodes[0].Status, r.Nodes[1].Status, r.Nodes[2].Status)
	}
	if r.Summary.Audited != 2 || r.Summary.Unreachable != 1 || len(r.Summary.FailingNodes) != 1 || r.Summary.FailingNodes[0] != "c" {
		t.Errorf("summary %+v", r.Summary)
	}
	if n := atomic.LoadInt32(calls); n != 3 {
		t.Fatalf("first run ssh calls = %d", n)
	}

	if again := get("/api/fleet/ssh"); !again.Cached || atomic.LoadInt32(calls) != 3 {
		t.Errorf("second call should be cached (cached=%v calls=%d)", again.Cached, atomic.LoadInt32(calls))
	}
	if fresh := get("/api/fleet/ssh?refresh=1"); fresh.Cached || atomic.LoadInt32(calls) != 6 {
		t.Errorf("refresh=1 should re-run (cached=%v calls=%d)", fresh.Cached, atomic.LoadInt32(calls))
	}
	// A subset is its own cache entry; target order does not matter.
	get("/api/fleet/ssh?targets=n-c,a")
	if sub := get("/api/fleet/ssh?targets=a,c"); !sub.Cached || len(sub.Nodes) != 2 || atomic.LoadInt32(calls) != 8 {
		t.Errorf("subset cache: cached=%v nodes=%d calls=%d", sub.Cached, len(sub.Nodes), atomic.LoadInt32(calls))
	}
	// An empty targets value means the whole fleet.
	if all := get("/api/fleet/ssh?targets="); len(all.Nodes) != 3 || !all.Cached {
		t.Errorf("empty targets: nodes=%d cached=%v", len(all.Nodes), all.Cached)
	}
}

func TestHandleFleetSSHEmptyFleet(t *testing.T) {
	writeServers(t, `[]`)
	resetFleetSSHCache(t)
	stubFleetSSH(t, nil)
	rec := httptest.NewRecorder()
	handleFleetSSH(rec, httptest.NewRequest(http.MethodGet, "/api/fleet/ssh", nil))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"nodes":[]`) || !strings.Contains(rec.Body.String(), `"avg_score":null`) {
		t.Errorf("empty fleet: %d %s", rec.Code, rec.Body.String())
	}
}

func TestFleetSSHCacheExpiresAndSharesInFlightRuns(t *testing.T) {
	c := newFleetSSHCache(time.Minute)
	now := time.Unix(1000, 0)
	c.now = func() time.Time { return now }
	var runs int32
	release := make(chan struct{})
	run := func() FleetSSHReport {
		atomic.AddInt32(&runs, 1)
		<-release
		return FleetSSHReport{DurationMs: 1}
	}

	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, cached := c.get("k", false, run); cached {
				t.Error("an in-flight result is not a cached one")
			}
		}()
	}
	// Let the goroutines reach the cache before the run finishes.
	for atomic.LoadInt32(&runs) == 0 {
		time.Sleep(time.Millisecond)
	}
	time.Sleep(20 * time.Millisecond)
	close(release)
	wg.Wait()
	if n := atomic.LoadInt32(&runs); n != 1 {
		t.Fatalf("concurrent identical requests ran %d times", n)
	}

	if _, cached := c.get("k", false, run); !cached {
		t.Error("fresh entry not served from cache")
	}
	now = now.Add(61 * time.Second)
	if _, cached := c.get("k", false, run); cached || atomic.LoadInt32(&runs) != 2 {
		t.Errorf("expired entry served (runs=%d)", atomic.LoadInt32(&runs))
	}
}

func TestAuditFleetSSHBoundsConcurrency(t *testing.T) {
	var cur, peak int32
	orig := fleetSSHExec
	fleetSSHExec = func(host, cmd string, timeout int) (int, string, string, int64) {
		n := atomic.AddInt32(&cur, 1)
		for {
			p := atomic.LoadInt32(&peak)
			if n <= p || atomic.CompareAndSwapInt32(&peak, p, n) {
				break
			}
		}
		time.Sleep(5 * time.Millisecond)
		atomic.AddInt32(&cur, -1)
		return 255, "", "down", 1
	}
	t.Cleanup(func() { fleetSSHExec = orig })
	var fleet []FleetNode
	for i := 0; i < 30; i++ {
		fleet = append(fleet, FleetNode{ID: "s" + string(rune('a'+i))})
	}
	r := AuditFleetSSH(fleet)
	if len(r.Nodes) != 30 || r.Summary.Unreachable != 30 {
		t.Fatalf("nodes %d unreachable %d", len(r.Nodes), r.Summary.Unreachable)
	}
	if p := atomic.LoadInt32(&peak); p > fleetSSHConcurrency {
		t.Errorf("peak concurrency %d > %d", p, fleetSSHConcurrency)
	}
}

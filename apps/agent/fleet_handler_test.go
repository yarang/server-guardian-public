package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

var testNodes = []FleetNode{
	{ID: "site-a-arm1", Name: "a-arm1", Hostname: "10.0.0.5", Group: "site-a"},
	{ID: "site-a-arm2", Name: "a-arm2", Hostname: "10.0.0.6", Group: "site-a"},
	{ID: "cloud-x1", Name: "x1", Hostname: "x1.example.com", Group: "cloud"},
}

func TestResolveTargetHost(t *testing.T) {
	cases := map[string]string{
		"site-a-arm1": "site-a-arm1",
		"cloud-x1":    "cloud-x1",
	}
	for input, wantID := range cases {
		if got, err := resolveTargetHost(testNodes, input); err != nil || got != wantID {
			t.Errorf("resolveTargetHost(%q) = %q, %v; want %q", input, got, err, wantID)
		}
	}
	for _, bad := range []string{
		"", "-oProxyCommand=sh -c 'id>/tmp/p'", "-oProxyCommand=id", "-F/tmp/evil",
		"evil.example.com", "SITE-A-ARM1", " site-a-arm1", "site-a-arm1 -v", "site-a",
		"root@10.0.0.5", "10.0.0.5\n",
	} {
		if _, err := resolveTargetHost(testNodes, bad); !errors.Is(err, errUnknownHost) {
			t.Errorf("resolveTargetHost(%q) err = %v, want errUnknownHost", bad, err)
		}
	}
}

// A name or hostname is no longer a target; the error names the id that matches.
func TestResolveTargetHostIDOnlyWithHint(t *testing.T) {
	for input, wantID := range map[string]string{"a-arm2": "site-a-arm2", "x1.example.com": "cloud-x1", "10.0.0.5": "site-a-arm1"} {
		got, err := resolveTargetHost(testNodes, input)
		if got != "" || !errors.Is(err, errUnknownHost) || !strings.Contains(err.Error(), `"`+wantID+`"`) {
			t.Errorf("resolveTargetHost(%q) = %q, %v; want errUnknownHost naming %q", input, got, err, wantID)
		}
	}
}

func TestResolveTargetHostRejectsOptionLikeConfigEntry(t *testing.T) {
	nodes := []FleetNode{{ID: "-oProxyCommand=id"}}
	if _, err := resolveTargetHost(nodes, "-oProxyCommand=id"); err == nil {
		t.Fatal("option-like host accepted even though servers.json lists it")
	}
}

func TestSSHArgs(t *testing.T) {
	got := sshArgs("site-a-arm1", "uptime")
	want := []string{"-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=accept-new", "--", "site-a-arm1", "uptime"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("sshArgs = %q, want %q", got, want)
	}
	// Even a hostile host stays a positional argument after "--".
	args := sshArgs("-oProxyCommand=id", "uptime")
	if args[len(args)-3] != "--" || args[len(args)-2] != "-oProxyCommand=id" {
		t.Fatalf("host not after --: %q", args)
	}
}

func TestExecuteSSHRefusesOptionLikeHost(t *testing.T) {
	code, stdout, stderr, _ := executeSSH("-oProxyCommand=touch /tmp/guardian-pwned", "true", 1)
	if code == 0 || stdout != "" || stderr == "" {
		t.Fatalf("executeSSH ran an option-like host: code=%d stdout=%q stderr=%q", code, stdout, stderr)
	}
}

func TestSelectNodes(t *testing.T) {
	ids := func(ss []FleetNode) []string {
		var out []string
		for _, s := range ss {
			out = append(out, s.ID)
		}
		return out
	}
	all, unknown := selectNodes(testNodes, nil)
	if len(all) != 3 || unknown != nil {
		t.Fatalf("no targets: %v %v", ids(all), unknown)
	}
	got, unknown := selectNodes(testNodes, []string{"a-arm2", " x1 "})
	if !reflect.DeepEqual(ids(got), []string{"site-a-arm2", "cloud-x1"}) || unknown != nil {
		t.Fatalf("id+name: %v %v", ids(got), unknown)
	}
	// The legacy servers.json group string is not a target any more: node groups are resolved
	// to explicit ids by the caller.
	if got, unknown = selectNodes(testNodes, []string{"site-a"}); len(got) != 0 || !reflect.DeepEqual(unknown, []string{"site-a"}) {
		t.Fatalf("legacy group matched: %v %v", ids(got), unknown)
	}
	_, unknown = selectNodes(testNodes, []string{"site-a-arm1", "-oProxyCommand=id", "nope"})
	if !reflect.DeepEqual(unknown, []string{"-oProxyCommand=id", "nope"}) {
		t.Fatalf("unknown = %v", unknown)
	}
}

func writeServers(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "servers.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GUARDIAN_SERVERS_FILE", path)
	return path
}

func TestServersFileAndRuleFields(t *testing.T) {
	path := writeServers(t, `[{"id":"a","name":"n-a","group":"legacy","tier":"Tier-1","role":"llm_cli_host",
		"guardian_role":"hub","cli_support":["claude"],"tailscale_tags":["tag:fleet"]}]`)
	if got, err := serversFile(); err != nil || got != path {
		t.Fatalf("serversFile = %q, %v", got, err)
	}
	nodes, err := loadNodes()
	if err != nil {
		t.Fatal(err)
	}
	out, _ := json.Marshal(nodes[0])
	for _, want := range []string{`"tier":"Tier-1"`, `"role":"llm_cli_host"`, `"guardian_role":"hub"`, `"cli_support":["claude"]`} {
		if !strings.Contains(string(out), want) {
			t.Errorf("/api/nodes JSON lacks %s: %s", want, out)
		}
	}
}

func TestFleetTargetsAreValidatedBeforeAnyRun(t *testing.T) {
	writeServers(t, `[{"id":"a","name":"n-a"},{"id":"b","name":"n-b"}]`)
	got, err := fleetSubset([]string{"n-b"})
	if err != nil || len(got) != 1 || got[0].ID != "b" {
		t.Fatalf("fleetSubset: %v %v", got, err)
	}
	if all, err := fleetSubset(nil); err != nil || len(all) != 2 {
		t.Fatalf("fleetSubset(nil): %v %v", all, err)
	}
	// Unknown targets fail before any ssh starts and map to 400.
	if _, err := DiagnoseFleet([]string{"a", "zz"}); !errors.Is(err, errUnknownHost) || fleetErrorStatus(err) != http.StatusBadRequest {
		t.Errorf("DiagnoseFleet unknown target: %v", err)
	}
	if _, err := ExecFleet("id", 1, []string{"-oProxyCommand=x"}); !errors.Is(err, errUnknownHost) {
		t.Errorf("ExecFleet unknown target: %v", err)
	}
	if fleetErrorStatus(errors.New("servers.json not found")) != http.StatusInternalServerError {
		t.Error("non-target errors should be 500")
	}

	req := httptest.NewRequest(http.MethodPost, "/api/fleet/exec/stream", strings.NewReader(`{"command":"id","targets":["a","nope"]}`))
	rec := httptest.NewRecorder()
	handleFleetExecStream(rec, req)
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "nope") {
		t.Errorf("stream with unknown target: %d %s", rec.Code, rec.Body.String())
	}
}

func TestTargetsFromQuery(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/fleet/diagnose?targets=a,%20b,&targets=c", nil)
	if got := targetsFromQuery(req); !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
		t.Errorf("targetsFromQuery = %v", got)
	}
	req = httptest.NewRequest(http.MethodGet, "/api/fleet/diagnose", nil)
	if got := targetsFromQuery(req); got != nil {
		t.Errorf("no targets = %v", got)
	}
}

func TestSSHArgsHostKeyPolicyPerTarget(t *testing.T) {
	dir := t.TempDir()
	fleet := filepath.Join(dir, "fleet.conf")
	t.Setenv("GUARDIAN_SSH_FLEET_FILE", fleet)
	content := renderFleetBlock("pinned-1", "203.0.113.9", "ubuntu", 22, shapeRegistered) + "\n" +
		renderFleetBlock("migrated-1", "100.64.0.5", "ubuntu", 22, shapeMigrated) + "\n" +
		"Host hand-1\n    HostName 10.0.0.1\n    StrictHostKeyChecking yes\n"
	if err := os.WriteFile(fleet, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	hasAccept := func(host string) bool {
		args := sshArgsFor(host, "true", isPinnedHost(host))
		if args[len(args)-3] != "--" || args[len(args)-2] != host {
			t.Fatalf("-- / host guarantee broken: %q", args)
		}
		for _, a := range args {
			if a == "StrictHostKeyChecking=accept-new" {
				return true
			}
		}
		return false
	}
	if hasAccept("pinned-1") {
		t.Error("pinned node must not get an accept-new override")
	}
	if hasAccept("hand-1") {
		t.Error("a hand-written strict block must keep its own policy")
	}
	for _, h := range []string{"migrated-1", "no-block"} {
		if !hasAccept(h) {
			t.Errorf("%s must keep accept-new", h)
		}
	}
	// A missing fleet file means no blocks: unknown hosts keep accept-new.
	_ = os.Remove(fleet)
	if !hasAccept("pinned-1") {
		t.Error("missing fleet file must fall back to accept-new")
	}
	// An unreadable fleet file (a directory) fails closed: no override and no ssh run.
	if err := os.Mkdir(fleet, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := hostKeyOverride("pinned-1"); err == nil {
		t.Fatal("unreadable fleet file must be an error")
	}
	code, _, stderr, _ := executeSSH("pinned-1", "true", 1)
	if code != 255 || !strings.Contains(stderr, "refusing to connect") {
		t.Errorf("executeSSH must refuse, got %d %q", code, stderr)
	}
}

// With GUARDIAN_SERVERS_FILE set the file is used or the call fails; the checkout fallbacks only
// apply when the variable is unset.
func TestServersFileEnvIsStrict(t *testing.T) {
	t.Setenv("GUARDIAN_SERVERS_FILE", filepath.Join(t.TempDir(), "gone.json"))
	_, err := serversFile()
	if err == nil || !strings.Contains(err.Error(), "gone.json") {
		t.Fatalf("missing env file must fail naming the path, got %v", err)
	}
	if _, err := loadNodes(); err == nil || fleetErrorStatus(err) != http.StatusInternalServerError {
		t.Errorf("loadNodes must fail with a 500-class error, got %v", err)
	}
	t.Setenv("GUARDIAN_SERVERS_FILE", t.TempDir())
	if _, err := serversFile(); err == nil || !strings.Contains(err.Error(), "directory") {
		t.Errorf("directory must fail, got %v", err)
	}
	// Unset: the dev fallback to configs/servers.json still works.
	t.Setenv("GUARDIAN_SERVERS_FILE", "")
	t.Chdir(t.TempDir())
	if err := os.MkdirAll("configs", 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile("configs/servers.json", []byte("[]"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got, err := serversFile(); err != nil || !strings.HasSuffix(got, "configs/servers.json") {
		t.Errorf("unset fallback = %q, %v", got, err)
	}
}

// Command endpoints reject an oversized body with 413 before touching the fleet.
func TestCommandBodyLimit(t *testing.T) {
	big := `{"command":"` + strings.Repeat("a", commandBodyLimit) + `"}`
	rec := httptest.NewRecorder()
	handleFleetExecStream(rec, httptest.NewRequest(http.MethodPost, "/api/fleet/exec/stream", strings.NewReader(big)))
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized body: status %d, want 413", rec.Code)
	}
	rec = httptest.NewRecorder()
	handleFleetExecStream(rec, httptest.NewRequest(http.MethodPost, "/api/fleet/exec/stream", strings.NewReader("{")))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("malformed body: status %d, want 400", rec.Code)
	}
}

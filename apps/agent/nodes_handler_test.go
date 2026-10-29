package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeSSH stands in for ssh, ssh-keyscan and the node probe. Nothing here touches the network.
type fakeSSH struct {
	mu       sync.Mutex
	calls    [][]string
	keyscan  string
	scanFail bool
	// gMismatch makes `ssh -G` report a different hostname than the fleet file holds.
	gMismatch bool
	fleetPath string
	probe     struct {
		code   int
		stdout string
		stderr string
	}
	probed []string
}

func (f *fakeSSH) run(name string, args []string, _ time.Duration) cmdResult {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, append([]string{name}, args...))
	switch name {
	case "ssh-keyscan":
		if f.scanFail {
			return cmdResult{Stderr: "connect: Connection refused"}
		}
		return cmdResult{Stdout: f.keyscan}
	case "ssh":
		if len(args) == 3 && args[0] == "-G" && args[1] == "--" {
			data, _ := os.ReadFile(f.fleetPath)
			blk, managed := parseFleetFile(string(data)).find(args[2])
			if blk == nil || !managed {
				return cmdResult{Stdout: "hostname " + args[2] + "\nuser root\nport 22\n"}
			}
			host := blk.HostName
			if f.gMismatch {
				host = "mismatch.example.com"
			}
			out := fmt.Sprintf("host %s\nhostname %s\nuser %s\nport %d\n", blk.Alias, host, blk.User, blk.Port)
			if blk.Shape == shapeRegistered {
				out += "hostkeyalias " + blk.Alias + "\n"
			}
			return cmdResult{Stdout: out}
		}
	}
	return cmdResult{Exit: 127, Stderr: "unexpected command " + name}
}

func (f *fakeSSH) callsTo(name string) [][]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out [][]string
	for _, c := range f.calls {
		if c[0] == name {
			out = append(out, c)
		}
	}
	return out
}

const seedInventory = `[
  {"id":"hub-1","name":"hub-1","hostname":"hub.example.com","user":"ubuntu","port":22,"arch":"aarch64","group":"Core","key":"~/.ssh/fleet_key_5","recommended":true,"description":"Hub <primary> & more","tailscale_ip":"100.64.0.7","tailscale_tags":["tag:fleet"],"tier":"Tier-1","role":"llm_cli_host","cli_support":["claude"],"guardian_role":"hub","backup_replica":"legacy-1","future_field":{"x":[1,2,3]}},
  {"id":"legacy-1","name":"legacy-1","hostname":"legacy.example.com","user":"ubuntu","port":22,"arch":"x86_64","group":"Core","tier":"Tier-2","guardian_role":"node","custom":123}
]`

type nodeEnv struct {
	t         *testing.T
	home      string
	fleet     string
	known     string
	config    string
	inv       string
	pub       string
	ssh       *fakeSSH
	h         http.Handler
	seededInv string
}

const nodeTestToken = "test-token"

func newNodeEnv(t *testing.T) *nodeEnv {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	e := &nodeEnv{t: t, home: home, ssh: &fakeSSH{}}
	e.fleet = filepath.Join(home, ".ssh", "guardian_fleet.conf")
	e.known = filepath.Join(home, ".ssh", "guardian_known_hosts")
	e.config = filepath.Join(home, ".ssh", "config")
	e.pub = filepath.Join(home, ".ssh", "guardian_fleet.pub")
	e.inv = filepath.Join(home, ".guardian", "data", "servers.json")
	t.Setenv("GUARDIAN_SSH_FLEET_FILE", e.fleet)
	t.Setenv("GUARDIAN_SSH_KNOWN_HOSTS", e.known)
	t.Setenv("GUARDIAN_SSH_CONFIG", e.config)
	t.Setenv("GUARDIAN_FLEET_PUBKEY_FILE", e.pub)
	t.Setenv("GUARDIAN_SERVERS_FILE", e.inv)
	for path, content := range map[string]string{
		e.config: "Include ~/.ssh/guardian_fleet.conf\n\nHost *\n    ServerAliveInterval 30\n",
		e.pub:    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPUBKEY guardian-hub-fleet@hub\n",
		e.inv:    seedInventory,
	} {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	e.seededInv = seedInventory
	e.ssh.fleetPath = e.fleet
	e.ssh.keyscan = "# h:22 SSH-2.0-OpenSSH\nh ssh-ed25519 " + testEd25519Blob + "\nh ecdsa-sha2-nistp256 " + testECDSABlob + "\n"
	e.ssh.probe.stdout = "aarch64\nsudo=yes\n"

	oldRunner, oldProbe := commandRunner, sshProbe
	commandRunner = e.ssh.run
	sshProbe = func(host, cmd string, _ int) (int, string, string, int64) {
		e.ssh.mu.Lock()
		defer e.ssh.mu.Unlock()
		e.ssh.probed = append(e.ssh.probed, host+" "+cmd)
		return e.ssh.probe.code, e.ssh.probe.stdout, e.ssh.probe.stderr, 7
	}
	t.Cleanup(func() { commandRunner, sshProbe = oldRunner, oldProbe })

	mux := http.NewServeMux()
	registerNodeRoutes(mux)
	registerMeshRoutes(mux)
	mux.HandleFunc("/api/nodes", func(w http.ResponseWriter, r *http.Request) {
		nodes, err := loadNodesWithConnection()
		if err != nil {
			jsonResponse(w, 500, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, 200, map[string]any{"count": len(nodes), "nodes": nodes})
	})
	e.h = withAuth(mux, nodeTestToken)
	return e
}

// do sends an authenticated JSON request and decodes the object response.
func (e *nodeEnv) do(method, path string, body any) (*httptest.ResponseRecorder, map[string]any) {
	e.t.Helper()
	var rdr *bytes.Reader
	switch b := body.(type) {
	case nil:
		rdr = bytes.NewReader(nil)
	case string:
		rdr = bytes.NewReader([]byte(b))
	default:
		raw, err := json.Marshal(b)
		if err != nil {
			e.t.Fatal(err)
		}
		rdr = bytes.NewReader(raw)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Authorization", "Bearer "+nodeTestToken)
	if rdr.Len() > 0 {
		req.Header.Set("Content-Type", "application/json")
	}
	rec := httptest.NewRecorder()
	e.h.ServeHTTP(rec, req)
	var m map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &m)
	return rec, m
}

func (e *nodeEnv) read(path string) string {
	data, _ := os.ReadFile(path)
	return string(data)
}

func (e *nodeEnv) register(id string, extra map[string]any) (*httptest.ResponseRecorder, map[string]any) {
	body := map[string]any{"id": id, "hostname": "203.0.113.9", "user": "ubuntu", "port": 22, "host_key_fingerprint": testEd25519FP}
	for k, v := range extra {
		body[k] = v
	}
	return e.do("POST", "/api/nodes", body)
}

func (e *nodeEnv) inventoryEntries() []map[string]any {
	e.t.Helper()
	var entries []map[string]any
	if err := json.Unmarshal([]byte(e.read(e.inv)), &entries); err != nil {
		e.t.Fatalf("inventory is not valid JSON: %v", err)
	}
	return entries
}

func (e *nodeEnv) entry(id string) map[string]any {
	for _, m := range e.inventoryEntries() {
		if m["id"] == id {
			return m
		}
	}
	return nil
}

func wantStatus(t *testing.T, rec *httptest.ResponseRecorder, want int, body map[string]any) {
	t.Helper()
	if rec.Code != want {
		t.Fatalf("status = %d, want %d; body: %s", rec.Code, want, rec.Body.String())
	}
}

func TestNodeRoutesRequireSession(t *testing.T) {
	e := newNodeEnv(t)
	for _, c := range []struct{ method, path string }{
		{"GET", "/api/nodes/registration"}, {"POST", "/api/nodes/check"}, {"POST", "/api/nodes"},
		{"PUT", "/api/nodes/legacy-1"}, {"DELETE", "/api/nodes/legacy-1"}, {"POST", "/api/nodes/legacy-1/probe"},
	} {
		req := httptest.NewRequest(c.method, c.path, strings.NewReader("{}"))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		e.h.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("%s %s without credentials = %d, want 401", c.method, c.path, rec.Code)
		}
	}
	// A real session cookie works the same as the bearer token.
	req := httptest.NewRequest("GET", "/api/nodes/registration", nil)
	req.AddCookie(&http.Cookie{Name: sessionCookie, Value: newSessionValue(nodeTestToken, time.Now())})
	rec := httptest.NewRecorder()
	e.h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Errorf("session cookie request = %d", rec.Code)
	}
	if len(e.ssh.calls) != 0 {
		t.Error("an unauthenticated request reached ssh")
	}
}

// Without a hub tailscale_ip there is no authorized_keys line (an unrestricted fleet key would work
// from any address); a blocker message explains why instead.
func TestAuthorizedKeyLineNeedsHubIP(t *testing.T) {
	e := newNodeEnv(t)
	if line, problem := authorizedKeyLine(""); line != "" || !strings.Contains(problem, "tailscale_ip") {
		t.Errorf("no hub ip: line=%q problem=%q", line, problem)
	}
	if line, problem := authorizedKeyLine("not-an-ip"); line != "" || problem == "" {
		t.Errorf("bad hub ip: line=%q problem=%q", line, problem)
	}
	noIP := strings.Replace(seedInventory, `"tailscale_ip":"100.64.0.7",`, "", 1)
	if err := os.WriteFile(e.inv, []byte(noIP), 0o600); err != nil {
		t.Fatal(err)
	}
	rec, m := e.do("GET", "/api/nodes/registration", nil)
	wantStatus(t, rec, 200, m)
	if key, _ := m["fleet_public_key"].(string); key != "" {
		t.Errorf("fleet_public_key = %q, want empty", key)
	}
	if b, _ := m["fleet_public_key_blocker"].(string); !strings.Contains(b, "tailscale_ip") {
		t.Errorf("blocker = %v", m["fleet_public_key_blocker"])
	}
	e.ssh.probe.code, e.ssh.probe.stdout = 255, ""
	e.ssh.probe.stderr = "ubuntu@203.0.113.9: Permission denied (publickey).\n"
	rec, m = e.register("edge-1", nil)
	wantStatus(t, rec, 201, m)
	if m["authorized_key"] != "" || !strings.Contains(m["authorized_key_blocker"].(string), "tailscale_ip") {
		t.Errorf("probe response: authorized_key=%v blocker=%v", m["authorized_key"], m["authorized_key_blocker"])
	}
}

// The registration facts publish the form rules so the dashboard can mirror the server.
func TestRegistrationFactsPublishFormRules(t *testing.T) {
	e := newNodeEnv(t)
	rec, m := e.do("GET", "/api/nodes/registration", nil)
	wantStatus(t, rec, 200, m)
	v, ok := m["validation"].(map[string]any)
	if !ok {
		t.Fatalf("validation missing: %v", m)
	}
	if id := v["id"].(map[string]any); id["pattern"] != nodeIDRe.String() {
		t.Errorf("id rule = %v", id)
	}
	if u := v["user"].(map[string]any); u["pattern"] != nodeUserRe.String() {
		t.Errorf("user rule = %v", u)
	}
	meta := v["metadata"].(map[string]any)
	if tag := meta["tailscale_tags"].(map[string]any); tag["pattern"] != metaTagRe.String() || tag["max_items"] != float64(maxMetaList) {
		t.Errorf("tag rule = %v", tag)
	}
	if tier := meta["tier"].(map[string]any); tier["pattern"] != metaTierRe.String() {
		t.Errorf("tier rule = %v", tier)
	}
	// Every published pattern compiles and agrees with the validators.
	for _, re := range []string{nodeIDRe.String(), nodeUserRe.String(), metaTagRe.String(), metaTierRe.String()} {
		if _, err := regexp.Compile(re); err != nil {
			t.Errorf("pattern %q: %v", re, err)
		}
	}
}

func TestRegistrationFacts(t *testing.T) {
	e := newNodeEnv(t)
	rec, m := e.do("GET", "/api/nodes/registration", nil)
	wantStatus(t, rec, 200, m)
	key, _ := m["fleet_public_key"].(string)
	wantKey := `from="100.64.0.7",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPUBKEY guardian-hub-fleet@hub`
	if key != wantKey {
		t.Errorf("fleet_public_key = %q", key)
	}
	if m["include_line"] != "Include ~/.ssh/guardian_fleet.conf" || m["include_present"] != true || m["include_effective"] != true {
		t.Errorf("include fields: %v", m)
	}
	if m["can_register"] != true || len(m["blockers"].([]any)) != 0 || m["inventory_writable"] != true {
		t.Errorf("can_register/blockers: %v", m)
	}
	if m["fleet_file"] != e.fleet || m["hub_tailscale_ip"] != "100.64.0.7" {
		t.Errorf("paths/hub ip: %v", m)
	}

	// Include missing and an unmanaged block are reported.
	_ = os.WriteFile(e.config, []byte("Host *\n    User x\n"), 0o600)
	_ = os.WriteFile(e.fleet, []byte("Host hand\n    HostName 10.0.0.1\n"), 0o600)
	_, m = e.do("GET", "/api/nodes/registration", nil)
	if m["include_effective"] != false || m["can_register"] != false || len(m["blockers"].([]any)) != 1 {
		t.Errorf("missing include not reported: %v", m)
	}
	if u := m["unmanaged_hosts"].([]any); len(u) != 1 || u[0] != "hand" {
		t.Errorf("unmanaged_hosts = %v", u)
	}

	// Without GUARDIAN_SERVERS_FILE the inventory is read-only.
	t.Setenv("GUARDIAN_SERVERS_FILE", "")
	_, m = e.do("GET", "/api/nodes/registration", nil)
	if m["inventory_writable"] != false || m["can_register"] != false {
		t.Errorf("read-only inventory not reported: %v", m)
	}
}

func TestCheckReturnsFingerprintsAndWritesNothing(t *testing.T) {
	e := newNodeEnv(t)
	rec, m := e.do("POST", "/api/nodes/check", map[string]any{"id": "edge-1", "hostname": "203.0.113.9", "user": "ubuntu", "port": 2222})
	wantStatus(t, rec, 200, m)
	keys := m["keys"].([]any)
	if len(keys) != 2 || keys[0].(map[string]any)["fingerprint"] != testEd25519FP || keys[0].(map[string]any)["type"] != "ssh-ed25519" {
		t.Errorf("keys = %v", keys)
	}
	if _, leaked := keys[0].(map[string]any)["Blob"]; leaked {
		t.Error("raw key blob must not be sent to the client")
	}
	if m["id_exists"] != false || m["hostname"] != "203.0.113.9" || m["port"] != float64(2222) {
		t.Errorf("response = %v", m)
	}
	scan := e.ssh.callsTo("ssh-keyscan")
	if len(scan) != 1 || !reflect.DeepEqual(scan[0], []string{"ssh-keyscan", "-T", "5", "-p", "2222", "203.0.113.9"}) {
		t.Errorf("keyscan argv = %v", scan)
	}
	for _, p := range []string{e.fleet, e.known} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("check created %s", p)
		}
	}
	if e.read(e.inv) != e.seededInv {
		t.Error("check changed the inventory")
	}
	// An existing id is reported, not an error: the same call serves connection edits.
	_, m = e.do("POST", "/api/nodes/check", map[string]any{"id": "legacy-1", "hostname": "203.0.113.9", "user": "ubuntu"})
	if m["id_exists"] != true || m["port"] != float64(22) {
		t.Errorf("existing id / default port: %v", m)
	}
}

func TestCheckRejectsInjectionBeforeAnyCommand(t *testing.T) {
	e := newNodeEnv(t)
	cases := []map[string]any{
		{"id": "x", "hostname": "-oProxyCommand=id", "user": "ubuntu", "port": 22},
		{"id": "x", "hostname": "-v", "user": "ubuntu", "port": 22},
		{"id": "x", "hostname": "host\nProxyCommand id", "user": "ubuntu", "port": 22},
		{"id": "x", "hostname": "a b", "user": "ubuntu", "port": 22},
		{"id": "x", "hostname": "host%h", "user": "ubuntu", "port": 22},
		{"id": "x", "hostname": "hôst.example.com", "user": "ubuntu", "port": 22},
		{"id": "x", "hostname": "1.2.3.4", "user": "ubuntu\nPort 1", "port": 22},
		{"id": "x", "hostname": "1.2.3.4", "user": "ubuntu", "port": 0},
		{"id": "x", "hostname": "1.2.3.4", "user": "ubuntu", "port": 70000},
		{"id": "X", "hostname": "1.2.3.4", "user": "ubuntu", "port": 22},
		{"id": "x\nHost *", "hostname": "1.2.3.4", "user": "ubuntu", "port": 22},
		{"id": "x", "hostname": "", "user": "ubuntu", "port": 22},
	}
	for i, body := range cases {
		rec, m := e.do("POST", "/api/nodes/check", body)
		if rec.Code != 400 || m["code"] != "invalid_field" {
			t.Errorf("case %d (%v): status %d body %s", i, body, rec.Code, rec.Body.String())
		}
	}
	for _, raw := range []string{`{"id":"x","hostname":"1.2.3.4","user":"u","port":"22"}`, `{"id":"x","hostname":"1.2.3.4","user":"u","extra":1}`, `not json`, `{"id":"x"} {"id":"y"}`} {
		if rec, _ := e.do("POST", "/api/nodes/check", raw); rec.Code != 400 {
			t.Errorf("body %s accepted: %d", raw, rec.Code)
		}
	}
	if n := len(e.ssh.calls); n != 0 {
		t.Errorf("%d commands ran for invalid input: %v", n, e.ssh.calls)
	}
}

func TestCheckUnreachableHost(t *testing.T) {
	e := newNodeEnv(t)
	e.ssh.keyscan = "# nothing\n"
	rec, m := e.do("POST", "/api/nodes/check", map[string]any{"id": "x", "hostname": "203.0.113.9", "user": "ubuntu", "port": 22})
	if rec.Code != 502 || m["code"] != "unreachable" {
		t.Errorf("status %d body %s", rec.Code, rec.Body.String())
	}
}

func TestRegisterFlow(t *testing.T) {
	e := newNodeEnv(t)
	rec, m := e.register("edge-1", map[string]any{"metadata": map[string]any{
		"tier": "Tier-2", "role": "ssh_target_node", "cli_support": []string{"ssh_agent_remote_exec"},
		"tailscale_tags": []string{"tag:fleet"}, "description": "Edge <1> & co",
	}})
	wantStatus(t, rec, 201, m)

	fleet := e.read(e.fleet)
	blk, managed := parseFleetFile(fleet).find("edge-1")
	if !managed || blk.Shape != shapeRegistered || blk.HostName != "203.0.113.9" || blk.User != "ubuntu" || blk.Port != 22 {
		t.Fatalf("fleet file:\n%s", fleet)
	}
	if !strings.HasPrefix(fleet, "# Managed by Server Guardian") {
		t.Error("fleet file header missing")
	}
	if got := e.read(e.known); got != "edge-1 ssh-ed25519 "+testEd25519Blob+"\n" {
		t.Errorf("known hosts = %q (only the confirmed key, under the alias)", got)
	}
	for _, p := range []string{e.fleet, e.known, e.inv} {
		if st, err := os.Stat(p); err != nil || st.Mode().Perm() != 0o600 {
			t.Errorf("%s mode = %v %v", p, st.Mode().Perm(), err)
		}
	}
	ent := e.entry("edge-1")
	if ent == nil {
		t.Fatal("inventory entry missing")
	}
	if ent["name"] != "edge-1" || ent["tier"] != "Tier-2" || ent["guardian_role"] != "node" || ent["arch"] != "aarch64" || ent["description"] != "Edge <1> & co" {
		t.Errorf("entry = %v", ent)
	}
	if _, has := ent["hostname"]; has {
		t.Error("connection fields must live in the fleet file only")
	}
	if !strings.Contains(e.read(e.inv), "Edge <1> & co") {
		t.Error("description was HTML-escaped in servers.json")
	}

	node := m["node"].(map[string]any)
	if node["connection"] != "fleet_file" || node["host_key_mode"] != "pinned" || node["hostname"] != "203.0.113.9" || node["port"] != float64(22) || node["arch"] != "aarch64" {
		t.Errorf("node = %v", node)
	}
	probe := m["probe"].(map[string]any)
	if probe["status"] != "ok" || probe["arch"] != "aarch64" || probe["sudo"] != "yes" {
		t.Errorf("probe = %v", probe)
	}
	if !reflect.DeepEqual(e.ssh.probed, []string{"edge-1 " + probeCommand}) {
		t.Errorf("probed = %v", e.ssh.probed)
	}
	g := e.ssh.callsTo("ssh")
	if len(g) != 1 || !reflect.DeepEqual(g[0], []string{"ssh", "-G", "--", "edge-1"}) {
		t.Errorf("verification command = %v", g)
	}
	if _, has := m["authorized_key"]; has {
		t.Error("authorized_key only when the key is not installed")
	}
}

func TestRegisterKeepsInventoryUnknownFieldsAndOtherEntries(t *testing.T) {
	e := newNodeEnv(t)
	before := e.inventoryEntries()
	rec, m := e.register("edge-1", nil)
	wantStatus(t, rec, 201, m)
	// No metadata in the request: the server stores the default role, not the legacy "agent".
	if ent := e.entry("edge-1"); ent["guardian_role"] != "node" {
		t.Errorf("default guardian_role = %v, want node", ent["guardian_role"])
	}
	after := e.inventoryEntries()
	if len(after) != 3 {
		t.Fatalf("entries = %d, want 3", len(after))
	}
	for i := 0; i < 2; i++ {
		if !reflect.DeepEqual(before[i], after[i]) {
			t.Errorf("entry %d changed:\n%v\n%v", i, before[i], after[i])
		}
	}
	raw := e.read(e.inv)
	if !strings.Contains(raw, `"Hub <primary> & more"`) {
		t.Error("existing description was re-escaped")
	}
	// Key order of an untouched entry is preserved.
	i := func(k string) int { return strings.Index(raw, `"`+k+`"`) }
	if !(i("recommended") < i("description") && i("guardian_role") < i("backup_replica") && i("backup_replica") < i("future_field")) {
		t.Error("key order of the hub entry changed")
	}
	if !strings.HasPrefix(raw, "[\n  {") || strings.HasSuffix(raw, "\n") {
		t.Errorf("format: want two-space indent and the original (missing) trailing newline: %q ... %q", raw[:10], raw[len(raw)-5:])
	}
	// The last write (the probed arch) kept the content before it as .bak.
	var bak []map[string]any
	if err := json.Unmarshal([]byte(e.read(e.inv+".bak")), &bak); err != nil || len(bak) != 3 {
		t.Errorf("servers.json.bak = %q (%v)", e.read(e.inv+".bak"), err)
	} else if _, has := bak[2]["arch"]; has {
		t.Error("servers.json.bak should be the content before the arch was stored")
	}
	// The arch update of a later probe keeps unknown fields of the edited entry too.
	e.ssh.probe.stdout = "x86_64\n"
	rec, _ = e.do("POST", "/api/nodes/edge-1/probe", nil)
	wantStatus(t, rec, 200, nil)
	if e.entry("edge-1")["arch"] != "x86_64" || !reflect.DeepEqual(e.inventoryEntries()[0], before[0]) {
		t.Error("probe rewrite damaged the inventory")
	}
}

func TestRegisterRequiresConfirmedMatchingFingerprint(t *testing.T) {
	e := newNodeEnv(t)
	rec, m := e.register("edge-1", map[string]any{"host_key_fingerprint": ""})
	if rec.Code != 400 || m["code"] != "fingerprint_required" {
		t.Errorf("empty fingerprint: %d %s", rec.Code, rec.Body.String())
	}
	rec, m = e.register("edge-1", map[string]any{"host_key_fingerprint": "SHA256:" + strings.Repeat("A", 43)})
	if rec.Code != 409 || m["code"] != "fingerprint_mismatch" {
		t.Errorf("wrong fingerprint: %d %s", rec.Code, rec.Body.String())
	}
	if keys, _ := m["keys"].([]any); len(keys) != 2 {
		t.Errorf("mismatch response should list the presented keys: %v", m)
	}
	rec, _ = e.register("edge-1", map[string]any{"host_key_fingerprint": "not a fingerprint"})
	if rec.Code != 400 {
		t.Errorf("malformed fingerprint: %d", rec.Code)
	}
	for _, p := range []string{e.fleet, e.known} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("%s written after a failed confirmation", p)
		}
	}
	if e.read(e.inv) != e.seededInv {
		t.Error("inventory changed after a failed confirmation")
	}
}

func TestRegisterRollsBackWhenSSHResolvesDifferently(t *testing.T) {
	e := newNodeEnv(t)
	// Pre-existing content that must come back byte for byte.
	origFleet := "# mine\nHost hand\n    HostName 10.9.9.9\n"
	origKnown := "other ssh-ed25519 AAAAother\n"
	_ = os.WriteFile(e.fleet, []byte(origFleet), 0o600)
	_ = os.WriteFile(e.known, []byte(origKnown), 0o600)
	e.ssh.gMismatch = true
	rec, m := e.register("edge-1", nil)
	if rec.Code != 500 || m["code"] != "verify_failed" || !strings.Contains(m["error"].(string), "rolled back") {
		t.Fatalf("status %d body %s", rec.Code, rec.Body.String())
	}
	if e.read(e.fleet) != origFleet || e.read(e.known) != origKnown || e.read(e.inv) != e.seededInv {
		t.Errorf("files not restored:\nfleet=%q\nknown=%q\ninv unchanged=%v", e.read(e.fleet), e.read(e.known), e.read(e.inv) == e.seededInv)
	}
	if len(e.ssh.probed) != 0 {
		t.Error("a rolled-back registration must not be probed")
	}

	// With no previous files, rollback removes what was created.
	e2 := newNodeEnv(t)
	e2.ssh.gMismatch = true
	if rec, _ := e2.register("edge-1", nil); rec.Code != 500 {
		t.Fatalf("status %d", rec.Code)
	}
	for _, p := range []string{e2.fleet, e2.known} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("%s still exists after rollback", p)
		}
	}
	if e2.read(e2.inv) != e2.seededInv {
		t.Error("inventory not restored")
	}
}

func TestRegisterRefusals(t *testing.T) {
	t.Run("include missing", func(t *testing.T) {
		e := newNodeEnv(t)
		_ = os.WriteFile(e.config, []byte("Host *\n    User x\nInclude ~/.ssh/guardian_fleet.conf\n"), 0o600)
		rec, m := e.register("edge-1", nil)
		if rec.Code != 409 || m["code"] != "include_missing" || !strings.Contains(m["hint"].(string), "Include ~/.ssh/guardian_fleet.conf") {
			t.Errorf("%d %s", rec.Code, rec.Body.String())
		}
		if len(e.ssh.calls) != 0 {
			t.Error("commands ran although the Include is missing")
		}
	})
	t.Run("inventory not configured", func(t *testing.T) {
		e := newNodeEnv(t)
		t.Setenv("GUARDIAN_SERVERS_FILE", "")
		rec, m := e.register("edge-1", nil)
		if rec.Code != 409 || m["code"] != "inventory_not_configured" {
			t.Errorf("%d %s", rec.Code, rec.Body.String())
		}
	})
	t.Run("duplicate id and name", func(t *testing.T) {
		e := newNodeEnv(t)
		for _, id := range []string{"legacy-1", "hub-1"} {
			if rec, m := e.register(id, nil); rec.Code != 409 || m["code"] != "id_exists" {
				t.Errorf("%s: %d %s", id, rec.Code, rec.Body.String())
			}
		}
		wantStatus(t, func() *httptest.ResponseRecorder { r, _ := e.register("edge-1", nil); return r }(), 201, nil)
		if rec, m := e.register("edge-1", nil); rec.Code != 409 || m["code"] != "id_exists" {
			t.Errorf("second registration: %d %s", rec.Code, rec.Body.String())
		}
	})
	t.Run("unmanaged block with the same alias", func(t *testing.T) {
		e := newNodeEnv(t)
		hand := "Host edge-1\n    HostName 10.0.0.1\n    ProxyCommand nc %h %p\n"
		_ = os.WriteFile(e.fleet, []byte(hand), 0o600)
		rec, m := e.register("edge-1", nil)
		if rec.Code != 409 || m["code"] != "unmanaged_block" {
			t.Fatalf("%d %s", rec.Code, rec.Body.String())
		}
		if e.read(e.fleet) != hand || e.read(e.inv) != e.seededInv {
			t.Error("files changed despite the refusal")
		}
		// A wildcard pattern that names the alias counts too.
		_ = os.WriteFile(e.fleet, []byte("Host edge-1 other\n    User x\n"), 0o600)
		if rec, m := e.register("edge-1", nil); rec.Code != 409 || m["code"] != "unmanaged_block" {
			t.Errorf("multi-pattern block: %d %v", rec.Code, m)
		}
	})
	t.Run("invalid input never reaches ssh", func(t *testing.T) {
		e := newNodeEnv(t)
		rec, _ := e.register("edge-1", map[string]any{"hostname": "-oProxyCommand=id"})
		if rec.Code != 400 {
			t.Errorf("%d", rec.Code)
		}
		rec, _ = e.register("edge-1", map[string]any{"metadata": map[string]any{"guardian_role": "hub"}})
		if rec.Code != 400 {
			t.Errorf("hub role claim: %d", rec.Code)
		}
		rec, _ = e.register("edge-1", map[string]any{"metadata": map[string]any{"arch": "x86_64"}})
		if rec.Code != 400 {
			t.Errorf("typed arch: %d", rec.Code)
		}
		if len(e.ssh.calls) != 0 {
			t.Errorf("commands ran: %v", e.ssh.calls)
		}
	})
}

func TestRegisterProbeOutcomes(t *testing.T) {
	e := newNodeEnv(t)
	e.ssh.probe.code, e.ssh.probe.stdout = 255, ""
	e.ssh.probe.stderr = "ubuntu@203.0.113.9: Permission denied (publickey).\n"
	rec, m := e.register("edge-1", nil)
	wantStatus(t, rec, 201, m)
	if p := m["probe"].(map[string]any); p["status"] != "key_not_installed" || !strings.Contains(p["stderr"].(string), "Permission denied") {
		t.Errorf("probe = %v", p)
	}
	if m["authorized_key"] == nil || !strings.Contains(m["authorized_key"].(string), `from="100.64.0.7"`) {
		t.Errorf("authorized_key = %v", m["authorized_key"])
	}
	if _, has := e.entry("edge-1")["arch"]; has {
		t.Error("arch must not be stored when the probe failed")
	}
	if _, managed := parseFleetFile(e.read(e.fleet)).find("edge-1"); !managed {
		t.Error("a node with a missing key stays registered")
	}

	// "Test again": unreachable, then ok.
	e.ssh.probe.stderr = "ssh: connect to host 203.0.113.9 port 22: Connection timed out\n"
	_, m = e.do("POST", "/api/nodes/edge-1/probe", nil)
	if p := m["probe"].(map[string]any); p["status"] != "unreachable" {
		t.Errorf("probe = %v", p)
	}
	e.ssh.probe.code, e.ssh.probe.stdout, e.ssh.probe.stderr = 0, "x86_64\n", ""
	_, m = e.do("POST", "/api/nodes/edge-1/probe", nil)
	if p := m["probe"].(map[string]any); p["status"] != "ok" || p["arch"] != "x86_64" || e.entry("edge-1")["arch"] != "x86_64" {
		t.Errorf("probe = %v entry = %v", p, e.entry("edge-1"))
	}
	if rec, _ := e.do("POST", "/api/nodes/ghost/probe", nil); rec.Code != 404 {
		t.Errorf("unknown id probe = %d", rec.Code)
	}
}

func TestUpdateMetadataOnly(t *testing.T) {
	e := newNodeEnv(t)
	wantStatus(t, func() *httptest.ResponseRecorder {
		r, _ := e.register("edge-1", map[string]any{"metadata": map[string]any{"tier": "Tier-2", "description": "old"}})
		return r
	}(), 201, nil)
	fleetBefore := e.read(e.fleet)
	e.ssh.mu.Lock()
	e.ssh.calls, e.ssh.probed = nil, nil
	e.ssh.mu.Unlock()

	rec, m := e.do("PUT", "/api/nodes/edge-1", map[string]any{"metadata": map[string]any{"tier": "Tier-1", "description": nil, "cli_support": []string{"claude"}, "ops_roles": []string{"web", "database"}}})
	wantStatus(t, rec, 200, m)
	ent := e.entry("edge-1")
	if ent["tier"] != "Tier-1" || ent["description"] != nil || !reflect.DeepEqual(ent["cli_support"], []any{"claude"}) ||
		!reflect.DeepEqual(ent["ops_roles"], []any{"web", "database"}) {
		t.Errorf("entry = %v", ent)
	}
	if _, has := ent["description"]; has {
		t.Error("null must delete the key")
	}
	if e.read(e.fleet) != fleetBefore {
		t.Error("metadata edit rewrote the fleet file")
	}
	if len(e.ssh.calls) != 0 || len(e.ssh.probed) != 0 {
		t.Errorf("metadata edit ran commands: %v %v", e.ssh.calls, e.ssh.probed)
	}
	if _, has := m["probe"]; has {
		t.Error("no probe expected for a metadata edit")
	}
	// Legacy entries keep their unknown fields.
	rec, _ = e.do("PUT", "/api/nodes/legacy-1", map[string]any{"metadata": map[string]any{"role": "ssh_target_node"}})
	wantStatus(t, rec, 200, nil)
	if l := e.entry("legacy-1"); l["custom"] != float64(123) || l["role"] != "ssh_target_node" || l["hostname"] != "legacy.example.com" {
		t.Errorf("legacy-1 = %v", l)
	}
}

func TestUpdateHubRoleProtected(t *testing.T) {
	e := newNodeEnv(t)
	for _, v := range []any{"node", nil} {
		rec, m := e.do("PUT", "/api/nodes/hub-1", map[string]any{"metadata": map[string]any{"guardian_role": v}})
		if rec.Code != 400 || m["field"] != "metadata.guardian_role" {
			t.Errorf("hub guardian_role=%v: %d %s", v, rec.Code, rec.Body.String())
		}
	}
	rec, _ := e.do("PUT", "/api/nodes/legacy-1", map[string]any{"metadata": map[string]any{"guardian_role": "hub"}})
	if rec.Code != 400 {
		t.Errorf("claiming hub = %d", rec.Code)
	}
	rec, _ = e.do("PUT", "/api/nodes/hub-1", map[string]any{"metadata": map[string]any{"tier": "Tier-1", "guardian_role": "hub"}})
	wantStatus(t, rec, 200, nil)
	if rec, _ := e.do("PUT", "/api/nodes/ghost", map[string]any{"metadata": map[string]any{}}); rec.Code != 404 {
		t.Errorf("unknown id = %d", rec.Code)
	}
}

func TestUpdateConnection(t *testing.T) {
	e := newNodeEnv(t)
	wantStatus(t, func() *httptest.ResponseRecorder { r, _ := e.register("edge-1", nil); return r }(), 201, nil)
	reset := func() {
		e.ssh.mu.Lock()
		e.ssh.calls, e.ssh.probed = nil, nil
		e.ssh.mu.Unlock()
	}

	// A new port needs a fingerprint confirmed for that port.
	reset()
	rec, m := e.do("PUT", "/api/nodes/edge-1", map[string]any{"port": 2222})
	if rec.Code != 400 || m["code"] != "fingerprint_required" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	rec, m = e.do("PUT", "/api/nodes/edge-1", map[string]any{"port": 2222, "host_key_fingerprint": "SHA256:" + strings.Repeat("B", 43)})
	if rec.Code != 409 || m["code"] != "fingerprint_mismatch" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	if blk, _ := parseFleetFile(e.read(e.fleet)).find("edge-1"); blk.Port != 22 {
		t.Error("fleet file changed without a confirmed fingerprint")
	}

	rec, m = e.do("PUT", "/api/nodes/edge-1", map[string]any{"hostname": "198.51.100.4", "port": 2222, "host_key_fingerprint": testECDSAFP})
	wantStatus(t, rec, 200, m)
	blk, managed := parseFleetFile(e.read(e.fleet)).find("edge-1")
	if !managed || blk.HostName != "198.51.100.4" || blk.Port != 2222 || blk.Shape != shapeRegistered {
		t.Errorf("block = %+v", blk)
	}
	if got := e.read(e.known); got != "edge-1 ecdsa-sha2-nistp256 "+testECDSABlob+"\n" {
		t.Errorf("known hosts must hold only the newly confirmed key: %q", got)
	}
	if p := m["probe"].(map[string]any); p["status"] != "ok" {
		t.Errorf("connection change must be probed: %v", m["probe"])
	}
	if n := m["node"].(map[string]any); n["hostname"] != "198.51.100.4" || n["port"] != float64(2222) {
		t.Errorf("node = %v", n)
	}

	// Only the user changes: no fingerprint, pin kept.
	known := e.read(e.known)
	rec, m = e.do("PUT", "/api/nodes/edge-1", map[string]any{"user": "deploy"})
	wantStatus(t, rec, 200, m)
	blk, _ = parseFleetFile(e.read(e.fleet)).find("edge-1")
	if blk.User != "deploy" || blk.HostName != "198.51.100.4" || e.read(e.known) != known {
		t.Errorf("user change: %+v", blk)
	}

	// Identical values change nothing and run nothing.
	reset()
	before := e.read(e.fleet)
	rec, m = e.do("PUT", "/api/nodes/edge-1", map[string]any{"user": "deploy", "hostname": "198.51.100.4", "port": 2222})
	wantStatus(t, rec, 200, m)
	if e.read(e.fleet) != before || len(e.ssh.calls) != 0 {
		t.Error("no-op connection update wrote or ran something")
	}

	// Invalid values and a failed verification leave everything as it was.
	for _, body := range []map[string]any{{"hostname": "-oProxyCommand=id"}, {"user": "a\nb"}, {"port": 0}} {
		if rec, _ := e.do("PUT", "/api/nodes/edge-1", body); rec.Code != 400 {
			t.Errorf("%v accepted: %d", body, rec.Code)
		}
	}
	e.ssh.gMismatch = true
	fleetBefore, knownBefore := e.read(e.fleet), e.read(e.known)
	rec, m = e.do("PUT", "/api/nodes/edge-1", map[string]any{"user": "other"})
	if rec.Code != 500 || m["code"] != "verify_failed" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	if e.read(e.fleet) != fleetBefore || e.read(e.known) != knownBefore {
		t.Error("files not restored after failed verification")
	}
}

func TestUpdateKeepsMigratedShapeUnlessHostChanges(t *testing.T) {
	e := newNodeEnv(t)
	migrated := "Host legacy-1\n" + strings.SplitN(renderFleetBlock("legacy-1", "100.64.0.5", "ubuntu", 22, shapeMigrated), "\n", 2)[1]
	_ = os.WriteFile(e.fleet, []byte(migrated), 0o600)
	rec, m := e.do("PUT", "/api/nodes/legacy-1", map[string]any{"user": "deploy"})
	wantStatus(t, rec, 200, m)
	blk, _ := parseFleetFile(e.read(e.fleet)).find("legacy-1")
	if blk.Shape != shapeMigrated || blk.User != "deploy" || blk.HostName != "100.64.0.5" {
		t.Errorf("block = %+v", blk)
	}
	if _, err := os.Stat(e.known); !os.IsNotExist(err) {
		t.Error("known hosts written for a user-only change")
	}
	rec, _ = e.do("PUT", "/api/nodes/legacy-1", map[string]any{"hostname": "198.51.100.4", "host_key_fingerprint": testEd25519FP})
	wantStatus(t, rec, 200, nil)
	if blk, _ = parseFleetFile(e.read(e.fleet)).find("legacy-1"); blk.Shape != shapeRegistered || blk.HostName != "198.51.100.4" {
		t.Errorf("re-confirmed block = %+v", blk)
	}

	// A node that only exists in the inventory gets a block once a host key is confirmed.
	rec, m = e.do("PUT", "/api/nodes/hub-1", map[string]any{"hostname": "203.0.113.9", "user": "ubuntu"})
	if rec.Code != 400 || m["code"] != "fingerprint_required" {
		t.Errorf("inventory-only node without fingerprint: %d %s", rec.Code, rec.Body.String())
	}
}

func TestUpdateRefusesUnmanagedBlockForConnection(t *testing.T) {
	e := newNodeEnv(t)
	hand := "Host legacy-1\n    HostName 10.0.0.1\n    ProxyJump bastion\n"
	_ = os.WriteFile(e.fleet, []byte(hand), 0o600)
	rec, m := e.do("PUT", "/api/nodes/legacy-1", map[string]any{"user": "deploy"})
	if rec.Code != 409 || m["code"] != "unmanaged_block" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	if e.read(e.fleet) != hand {
		t.Error("unmanaged block was modified")
	}
	// Metadata lives in servers.json and stays editable.
	rec, _ = e.do("PUT", "/api/nodes/legacy-1", map[string]any{"metadata": map[string]any{"tier": "Tier-3"}})
	wantStatus(t, rec, 200, nil)
}

func TestDeleteFlow(t *testing.T) {
	e := newNodeEnv(t)
	_ = os.WriteFile(e.known, []byte("other ssh-ed25519 AAAAother\n"), 0o600)
	wantStatus(t, func() *httptest.ResponseRecorder { r, _ := e.register("edge-1", nil); return r }(), 201, nil)
	wantStatus(t, func() *httptest.ResponseRecorder {
		r, _ := e.register("edge-2", map[string]any{"hostname": "203.0.113.10"})
		return r
	}(), 201, nil)

	rec, m := e.do("DELETE", "/api/nodes/edge-1", nil)
	wantStatus(t, rec, 200, m)
	removed := m["removed"].(map[string]any)
	if removed["fleet_block"] != true || removed["known_hosts"] != true || removed["inventory"] != true {
		t.Errorf("removed = %v", removed)
	}
	ff := parseFleetFile(e.read(e.fleet))
	if blk, _ := ff.find("edge-1"); blk != nil {
		t.Error("block still in fleet file")
	}
	if _, managed := ff.find("edge-2"); !managed {
		t.Error("another node's block was removed")
	}
	if got := e.read(e.known); strings.Contains(got, "edge-1 ") || !strings.Contains(got, "other ssh-ed25519") || !strings.Contains(got, "edge-2 ssh-ed25519") {
		t.Errorf("known hosts = %q", got)
	}
	if e.entry("edge-1") != nil || e.entry("edge-2") == nil || e.entry("legacy-1") == nil {
		t.Errorf("inventory ids = %v", e.inventoryEntries())
	}
	if rec, _ := e.do("DELETE", "/api/nodes/edge-1", nil); rec.Code != 404 {
		t.Errorf("second delete = %d", rec.Code)
	}
}

func TestDeleteRefusals(t *testing.T) {
	e := newNodeEnv(t)
	rec, m := e.do("DELETE", "/api/nodes/hub-1", nil)
	if rec.Code != 403 || m["code"] != "hub_protected" {
		t.Errorf("hub delete: %d %s", rec.Code, rec.Body.String())
	}
	if e.read(e.inv) != e.seededInv {
		t.Error("hub refusal changed the inventory")
	}
	if rec, _ := e.do("DELETE", "/api/nodes/nobody", nil); rec.Code != 404 {
		t.Errorf("unknown id: %d", rec.Code)
	}
	for _, bad := range []string{"..%2Fx", "A", "a%0Ab"} {
		if rec, _ := e.do("DELETE", "/api/nodes/"+bad, nil); rec.Code != 400 && rec.Code != 404 {
			t.Errorf("DELETE %s = %d", bad, rec.Code)
		}
	}
	hand := "Host legacy-1\n    HostName 10.0.0.1\n    ProxyCommand nc %h %p\n"
	_ = os.WriteFile(e.fleet, []byte(hand), 0o600)
	rec, m = e.do("DELETE", "/api/nodes/legacy-1", nil)
	if rec.Code != 409 || m["code"] != "unmanaged_block" {
		t.Errorf("unmanaged delete: %d %s", rec.Code, rec.Body.String())
	}
	if e.read(e.fleet) != hand || e.entry("legacy-1") == nil {
		t.Error("unmanaged delete changed something")
	}
	// Inventory-only legacy node: only its entry goes.
	_ = os.Remove(e.fleet)
	rec, m = e.do("DELETE", "/api/nodes/legacy-1", nil)
	wantStatus(t, rec, 200, m)
	if r := m["removed"].(map[string]any); r["inventory"] != true || r["fleet_block"] != false {
		t.Errorf("removed = %v", r)
	}
}

func TestNodesOverlay(t *testing.T) {
	e := newNodeEnv(t)
	migrated := renderFleetBlock("legacy-1", "100.64.0.5", "deploy", 2200, shapeMigrated)
	_ = os.WriteFile(e.fleet, []byte(migrated), 0o600)
	wantStatus(t, func() *httptest.ResponseRecorder { r, _ := e.register("edge-1", nil); return r }(), 201, nil)

	_, m := e.do("GET", "/api/nodes", nil)
	byID := map[string]map[string]any{}
	for _, s := range m["nodes"].([]any) {
		sm := s.(map[string]any)
		byID[sm["id"].(string)] = sm
	}
	if len(byID) != 3 {
		t.Fatalf("nodes = %v", byID)
	}
	hub := byID["hub-1"]
	if hub["connection"] != "inventory" || hub["hostname"] != "hub.example.com" || hub["host_key_mode"] != nil {
		t.Errorf("hub = %v", hub)
	}
	leg := byID["legacy-1"]
	if leg["connection"] != "fleet_file" || leg["hostname"] != "100.64.0.5" || leg["user"] != "deploy" || leg["port"] != float64(2200) || leg["host_key_mode"] != "accept-new" {
		t.Errorf("legacy-1 overlay = %v", leg)
	}
	edge := byID["edge-1"]
	if edge["connection"] != "fleet_file" || edge["host_key_mode"] != "pinned" || edge["hostname"] != "203.0.113.9" {
		t.Errorf("edge-1 = %v", edge)
	}

	// The allow-list for command targets still comes from servers.json (loadNodes), so a
	// request cannot steer ssh to a fleet-file-only hostname.
	nodes, _ := loadNodes()
	if _, err := resolveTargetHost(nodes, "203.0.113.9"); err == nil {
		t.Error("fleet file hostname must not become a valid target_host")
	}
	if got, err := resolveTargetHost(nodes, "edge-1"); err != nil || got != "edge-1" {
		t.Errorf("registered node id = %q, %v", got, err)
	}
	// A hostname is not a target; the error points at the node id.
	if _, err := resolveTargetHost(nodes, "legacy.example.com"); err == nil || !strings.Contains(err.Error(), `"legacy-1"`) {
		t.Errorf("inventory hostname err = %v; want a hint naming legacy-1", err)
	}
}

func TestConcurrentRegistrationsKeepEveryNode(t *testing.T) {
	e := newNodeEnv(t)
	var wg sync.WaitGroup
	codes := make([]int, 8)
	for i := range codes {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rec, _ := e.register("node-"+strconv.Itoa(i), map[string]any{"hostname": "203.0.113." + strconv.Itoa(10+i)})
			codes[i] = rec.Code
		}(i)
	}
	wg.Wait()
	ff := parseFleetFile(e.read(e.fleet))
	if got := len(e.inventoryEntries()); got != 2+len(codes) {
		t.Errorf("inventory entries = %d", got)
	}
	for i, c := range codes {
		id := "node-" + strconv.Itoa(i)
		if c != 201 {
			t.Errorf("%s status %d", id, c)
		}
		if _, managed := ff.find(id); !managed {
			t.Errorf("%s missing from the fleet file", id)
		}
		if !strings.Contains(e.read(e.known), id+" ssh-ed25519 ") {
			t.Errorf("%s missing from known hosts", id)
		}
	}
}

func TestCORSPreflightAllowsPUT(t *testing.T) {
	h := withSecurity(http.NotFoundHandler(), newSecurityConfig("", "", ""))
	req := httptest.NewRequest("OPTIONS", "/api/nodes/edge-1", nil)
	req.Host = "127.0.0.1:9870"
	req.Header.Set("Origin", "http://localhost:5173")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if m := rec.Header().Get("Access-Control-Allow-Methods"); !strings.Contains(m, "PUT") {
		t.Errorf("Access-Control-Allow-Methods = %q, want PUT included", m)
	}
}

func TestReservedNodeIDs(t *testing.T) {
	for _, id := range []string{"new", "registration", "check", "probe"} {
		if validateNodeID(id) == nil {
			t.Errorf("reserved id %q accepted", id)
		}
	}
	if validateNodeID("new-1") != nil || validateNodeID("checkpoint") != nil {
		t.Error("ids that merely start with a reserved word must stay valid")
	}
	e := newNodeEnv(t)
	rec, m := e.register("new", nil)
	if rec.Code != 400 || m["code"] != "invalid_field" || m["field"] != "id" {
		t.Errorf("register new: %d %s", rec.Code, rec.Body.String())
	}
	rec, m = e.do("POST", "/api/nodes/check", map[string]any{"id": "registration", "hostname": "203.0.113.9", "user": "ubuntu"})
	if rec.Code != 400 || m["field"] != "id" {
		t.Errorf("check registration: %d", rec.Code)
	}
	for _, c := range [][2]string{{"PUT", "/api/nodes/new"}, {"DELETE", "/api/nodes/new"}, {"POST", "/api/nodes/check/probe"}} {
		if rec, _ := e.do(c[0], c[1], map[string]any{}); rec.Code != 400 {
			t.Errorf("%s %s = %d, want 400", c[0], c[1], rec.Code)
		}
	}
	if len(e.ssh.calls) != 0 {
		t.Error("commands ran for reserved ids")
	}
}

func (e *nodeEnv) addInventory(entry map[string]any) {
	e.t.Helper()
	entries := e.inventoryEntries()
	entries = append(entries, entry)
	raw, _ := json.Marshal(entries)
	if err := os.WriteFile(e.inv, raw, 0o600); err != nil {
		e.t.Fatal(err)
	}
}

func TestReconfirmPinsMigratedBlock(t *testing.T) {
	e := newNodeEnv(t)
	_ = os.WriteFile(e.fleet, []byte(renderFleetBlock("legacy-1", "100.64.0.5", "deploy", 2200, shapeMigrated)), 0o600)
	inv := e.read(e.inv)

	// Wrong fingerprint: nothing changes.
	rec, m := e.do("PUT", "/api/nodes/legacy-1", map[string]any{"host_key_fingerprint": "SHA256:" + strings.Repeat("C", 43)})
	if rec.Code != 409 || m["code"] != "fingerprint_mismatch" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	if blk, _ := parseFleetFile(e.read(e.fleet)).find("legacy-1"); blk.Shape != shapeMigrated {
		t.Error("block changed without a matching fingerprint")
	}

	rec, m = e.do("PUT", "/api/nodes/legacy-1", map[string]any{"host_key_fingerprint": testEd25519FP})
	wantStatus(t, rec, 200, m)
	blk, managed := parseFleetFile(e.read(e.fleet)).find("legacy-1")
	if !managed || blk.Shape != shapeRegistered || blk.HostName != "100.64.0.5" || blk.User != "deploy" || blk.Port != 2200 {
		t.Errorf("block = %+v", blk)
	}
	if got := e.read(e.known); got != "legacy-1 ssh-ed25519 "+testEd25519Blob+"\n" {
		t.Errorf("known hosts = %q", got)
	}
	if n := m["node"].(map[string]any); n["host_key_mode"] != "pinned" || n["connection"] != "fleet_file" {
		t.Errorf("node = %v", n)
	}
	if p := m["probe"].(map[string]any); p["status"] != "ok" {
		t.Errorf("probe = %v", m["probe"])
	}
	if e.read(e.inv) != inv {
		// arch is stored by the probe; everything else must be as before
		if e.entry("legacy-1")["custom"] != float64(123) {
			t.Error("inventory entry damaged")
		}
	}
	if g := e.ssh.callsTo("ssh"); len(g) != 1 || g[0][1] != "-G" {
		t.Errorf("ssh -G verification missing: %v", g)
	}

	// Re-pinning an already pinned node replaces the key (rotated host key).
	rec, _ = e.do("PUT", "/api/nodes/legacy-1", map[string]any{"host_key_fingerprint": testECDSAFP})
	wantStatus(t, rec, 200, nil)
	if got := e.read(e.known); got != "legacy-1 ecdsa-sha2-nistp256 "+testECDSABlob+"\n" {
		t.Errorf("re-pin known hosts = %q", got)
	}
}

func TestReconfirmCreatesBlockForInventoryNode(t *testing.T) {
	e := newNodeEnv(t)
	e.addInventory(map[string]any{"id": "ts-1", "name": "ts-1", "hostname": "ts.example.com", "tailscale_ip": "100.64.0.9", "user": "deploy", "port": 2200})
	e.addInventory(map[string]any{"id": "pub-1", "name": "pub-1", "hostname": "pub.example.com", "tailscale_ip": "192.0.2.1"})
	e.addInventory(map[string]any{"id": "bare-1", "name": "bare-1"})

	rec, m := e.do("PUT", "/api/nodes/ts-1", map[string]any{"host_key_fingerprint": testEd25519FP})
	wantStatus(t, rec, 200, m)
	blk, managed := parseFleetFile(e.read(e.fleet)).find("ts-1")
	if !managed || blk.Shape != shapeRegistered || blk.HostName != "100.64.0.9" || blk.User != "deploy" || blk.Port != 2200 {
		t.Errorf("tailscale ip must be preferred: %+v", blk)
	}
	if n := m["node"].(map[string]any); n["connection"] != "fleet_file" || n["host_key_mode"] != "pinned" {
		t.Errorf("node = %v", n)
	}
	if m["probe"] == nil {
		t.Error("probe expected")
	}

	// Not a 100.64 address: hostname is used; user defaults to ubuntu, port to 22.
	rec, m = e.do("PUT", "/api/nodes/pub-1", map[string]any{"host_key_fingerprint": testEd25519FP})
	wantStatus(t, rec, 200, m)
	if blk, _ := parseFleetFile(e.read(e.fleet)).find("pub-1"); blk.HostName != "pub.example.com" || blk.User != "ubuntu" || blk.Port != 22 {
		t.Errorf("pub-1 = %+v", blk)
	}

	// Request values win over the inventory.
	rec, m = e.do("PUT", "/api/nodes/legacy-1", map[string]any{"host_key_fingerprint": testEd25519FP, "hostname": "198.51.100.4", "port": 2022})
	wantStatus(t, rec, 200, m)
	if blk, _ := parseFleetFile(e.read(e.fleet)).find("legacy-1"); blk.HostName != "198.51.100.4" || blk.Port != 2022 || blk.User != "ubuntu" {
		t.Errorf("legacy-1 = %+v", blk)
	}

	// No usable address at all, or an invalid one: 400 and nothing written.
	before := e.read(e.fleet)
	rec, m = e.do("PUT", "/api/nodes/bare-1", map[string]any{"host_key_fingerprint": testEd25519FP})
	if rec.Code != 400 || m["field"] != "hostname" {
		t.Errorf("bare-1: %d %s", rec.Code, rec.Body.String())
	}
	rec, _ = e.do("PUT", "/api/nodes/bare-1", map[string]any{"host_key_fingerprint": testEd25519FP, "hostname": "-oProxyCommand=id"})
	if rec.Code != 400 || e.read(e.fleet) != before {
		t.Errorf("injection: %d", rec.Code)
	}

	// Verification failure rolls everything back.
	e.ssh.gMismatch = true
	known := e.read(e.known)
	e.addInventory(map[string]any{"id": "rb-1", "name": "rb-1", "hostname": "rb.example.com"})
	rec, m = e.do("PUT", "/api/nodes/rb-1", map[string]any{"host_key_fingerprint": testEd25519FP})
	if rec.Code != 500 || m["code"] != "verify_failed" || e.read(e.fleet) != before || e.read(e.known) != known {
		t.Errorf("rollback: %d %s", rec.Code, rec.Body.String())
	}
}

func TestReconfirmRefusesUnmanagedBlock(t *testing.T) {
	e := newNodeEnv(t)
	hand := "Host legacy-1\n    HostName 10.0.0.1\n    ProxyJump bastion\n"
	_ = os.WriteFile(e.fleet, []byte(hand), 0o600)
	rec, m := e.do("PUT", "/api/nodes/legacy-1", map[string]any{"host_key_fingerprint": testEd25519FP})
	if rec.Code != 409 || m["code"] != "unmanaged_block" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	if e.read(e.fleet) != hand {
		t.Error("unmanaged block modified")
	}
	if _, err := os.Stat(e.known); !os.IsNotExist(err) {
		t.Error("known hosts written")
	}
}

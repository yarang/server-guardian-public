package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Real keys generated once with ssh-keygen; the fingerprints are what `ssh-keygen -lf` printed.
const (
	testEd25519Blob = "AAAAC3NzaC1lZDI1NTE5AAAAIGTcNIhGrFQuDMklJdllrKC+23Tk+tP0rkGAAPk01xXD"
	testEd25519FP   = "SHA256:D2jlepaBwAm9XpDI88W5nJ5CyrReaCfvqwJI/i4rQKk"
	testECDSABlob   = "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBLSe/uI4VKfC+RJp9/96mGPaKcXB/9YgSDkjF44cc5ERC5B7mHJ+7e2PxIDdthxDMnUyN8zri9ganmJ5mvQ0830="
	testECDSAFP     = "SHA256:L+5UBANOVSvPSFxRbAg3IvTiKCDBqHV66Pu4fG4X/D0"
)

func TestFingerprintFromKeyscanOutput(t *testing.T) {
	out := "# 203.0.113.9:22 SSH-2.0-OpenSSH_9.6\n" +
		"203.0.113.9 ssh-ed25519 " + testEd25519Blob + "\n" +
		"203.0.113.9 ecdsa-sha2-nistp256 " + testECDSABlob + "\n" +
		"203.0.113.9 ssh-ed25519 " + testEd25519Blob + "\n" + // duplicate
		"203.0.113.9 ssh-rsa " + testEd25519Blob + "\n" + // declared type disagrees with the blob
		"203.0.113.9 ssh-ed25519 not-base64!!\n" +
		"short line\n"
	keys := parseKeyscan(out)
	if len(keys) != 2 {
		t.Fatalf("keys = %+v, want 2 (ed25519, ecdsa)", keys)
	}
	if keys[0].Type != "ssh-ed25519" || keys[0].Fingerprint != testEd25519FP || keys[0].Blob != testEd25519Blob {
		t.Errorf("ed25519 key = %+v", keys[0])
	}
	if keys[1].Type != "ecdsa-sha2-nistp256" || keys[1].Fingerprint != testECDSAFP {
		t.Errorf("ecdsa key = %+v", keys[1])
	}
	if got := parseKeyscan("# only a banner\n"); len(got) != 0 {
		t.Errorf("banner-only output gave keys: %+v", got)
	}
}

const sampleFleet = `# hand-written header
# second line

Host site-0
    HostName 100.64.0.5
    User ubuntu
    Port 22
    IdentityFile ~/.ssh/guardian_fleet.pub
    IdentityAgent /run/guardian-fleet/agent.sock
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new

Host new-b
    HostName b.example.com
    User deploy
    Port 2222
    IdentityFile ~/.ssh/guardian_fleet.pub
    IdentityAgent /run/guardian-fleet/agent.sock
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    UserKnownHostsFile ~/.ssh/guardian_known_hosts
    HostKeyAlias new-b
# about the next block
Host evil
    HostName 10.0.0.1
    ProxyCommand sh -c 'id > /tmp/pwned'

Match host *.internal exec "true"
    User nobody

Host site-c
    HostName 100.64.0.9
    User ubuntu
    Port 22
    IdentityFile ~/.ssh/guardian_fleet.pub
    IdentityAgent /run/guardian-fleet/agent.sock
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    ProxyCommand sh -c 'id'
`

func TestParseFleetFileRoundTripAndShapes(t *testing.T) {
	ff := parseFleetFile(sampleFleet)
	if got := ff.render(); got != sampleFleet {
		t.Fatalf("render != input:\n%q\n%q", got, sampleFleet)
	}
	if !strings.HasPrefix(ff.Preamble, "# hand-written header") {
		t.Errorf("preamble = %q", ff.Preamble)
	}
	if len(ff.Blocks) != 5 {
		t.Fatalf("blocks = %d, want 5", len(ff.Blocks))
	}
	a, b := ff.Blocks[0], ff.Blocks[1]
	if !a.Managed || a.Shape != shapeMigrated || a.Alias != "site-0" || a.HostName != "100.64.0.5" || a.Port != 22 {
		t.Errorf("migrated block = %+v", a)
	}
	if !b.Managed || b.Shape != shapeRegistered || b.User != "deploy" || b.Port != 2222 || b.HostName != "b.example.com" {
		t.Errorf("registered block = %+v", b)
	}
	if b.Trailer != "# about the next block\n" {
		t.Errorf("trailer = %q", b.Trailer)
	}
	for _, i := range []int{2, 3, 4} {
		if ff.Blocks[i].Managed {
			t.Errorf("block %d must be unmanaged (ProxyCommand/Match/extra line): %q", i, ff.Blocks[i].Raw)
		}
	}
	if got := ff.unmanagedAliases(); len(got) != 2 || got[0] != "evil" || got[1] != "site-c" {
		t.Errorf("unmanagedAliases = %v", got)
	}
	if blk, managed := ff.find("evil"); blk == nil || managed {
		t.Errorf("find(evil) = %v, %v; want unmanaged block", blk, managed)
	}
	if blk, _ := ff.find("nope"); blk != nil {
		t.Errorf("find(nope) = %v", blk)
	}
}

func TestHandEditedBlocksStayUnmanaged(t *testing.T) {
	base := renderFleetBlock("n1", "h.example.com", "ubuntu", 22, shapeRegistered)
	if blk := parseFleetFile(base).Blocks[0]; !blk.Managed {
		t.Fatal("rendered block must parse as managed")
	}
	mutations := map[string]string{
		"extra directive": base + "    LocalCommand id\n",
		"identity file":   strings.Replace(base, "guardian_fleet.pub", "id_rsa", 1),
		"wrong alias":     strings.Replace(base, "HostKeyAlias n1", "HostKeyAlias other", 1),
		"crlf":            strings.ReplaceAll(base, "\n", "\r\n"),
		"port zero pad":   strings.Replace(base, "Port 22", "Port 022", 1),
		"bad hostname":    strings.Replace(base, "h.example.com", "h.example.com;id", 1),
		"tab indent":      strings.Replace(base, "    User", "\tUser", 1),
		"multi pattern":   strings.Replace(base, "Host n1", "Host n1 n2", 1),
		"lowercase key":   strings.Replace(base, "    HostName", "    hostname", 1),
		"inner comment":   strings.Replace(base, "    User ubuntu\n", "    # note\n    User ubuntu\n", 1),
	}
	for name, text := range mutations {
		blk := parseFleetFile(text).Blocks[0]
		if blk.Managed {
			t.Errorf("%s: parsed as managed", name)
		}
		if blk.Raw != text {
			t.Errorf("%s: raw bytes changed", name)
		}
	}
}

func TestFleetFileSetRemovePreservesUnmanaged(t *testing.T) {
	ff := parseFleetFile(sampleFleet)
	before := map[string]string{}
	for _, blk := range ff.Blocks {
		if !blk.Managed {
			before[blk.Raw] = blk.Raw
		}
	}
	ff.set("zz-new", "z.example.com", "ubuntu", 22, shapeRegistered)
	ff.set("new-b", "b2.example.com", "deploy", 2200, shapeRegistered)
	if !ff.remove("site-0") {
		t.Fatal("remove(site-0) = false")
	}
	if ff.remove("evil") {
		t.Fatal("remove must not touch unmanaged blocks")
	}
	out := parseFleetFile(ff.render())
	if blk, managed := out.find("zz-new"); !managed || blk.HostName != "z.example.com" {
		t.Errorf("zz-new = %+v", blk)
	}
	if blk, managed := out.find("new-b"); !managed || blk.Port != 2200 || !strings.Contains(blk.Trailer, "about the next block") {
		t.Errorf("new-b = %+v (trailer comment must survive a rewrite)", blk)
	}
	if blk, _ := out.find("site-0"); blk != nil {
		t.Error("site-0 still present")
	}
	for _, raw := range before {
		if !strings.Contains(out.render(), raw) {
			t.Errorf("unmanaged block changed or lost:\n%s", raw)
		}
	}
	if !strings.HasPrefix(out.render(), "# hand-written header\n# second line\n") {
		t.Error("preamble lost")
	}
	if out2 := parseFleetFile(out.render()); out2.render() != out.render() {
		t.Error("second parse/render is not stable")
	}
}

func TestFleetFileSetOnEmptyAddsHeaderAndSeparators(t *testing.T) {
	ff := parseFleetFile("")
	ff.set("a1", "10.0.0.1", "ubuntu", 22, shapeRegistered)
	ff.set("a2", "10.0.0.2", "ubuntu", 22, shapeRegistered)
	text := ff.render()
	if !strings.HasPrefix(text, "# Managed by Server Guardian") || !strings.Contains(text, "HostKeyAlias a1\n\nHost a2\n") {
		t.Fatalf("unexpected file:\n%s", text)
	}
	again := parseFleetFile(text)
	if len(again.Blocks) != 2 || !again.Blocks[0].Managed || !again.Blocks[1].Managed || again.render() != text {
		t.Fatalf("round trip failed:\n%s", text)
	}
	// A file whose last block has no final newline still gets a clean separator.
	ff = parseFleetFile("Host x\n    HostName x.example.com")
	ff.set("a1", "10.0.0.1", "ubuntu", 22, shapeMigrated)
	if !strings.Contains(ff.render(), "HostName x.example.com\n\nHost a1\n") {
		t.Fatalf("bad separator:\n%s", ff.render())
	}
}

func TestRenderFleetBlockShapes(t *testing.T) {
	want := "Host n1\n    HostName h.example.com\n    User ubuntu\n    Port 22\n" +
		"    IdentityFile ~/.ssh/guardian_fleet.pub\n    IdentityAgent /run/guardian-fleet/agent.sock\n    IdentitiesOnly yes\n" +
		"    StrictHostKeyChecking yes\n    UserKnownHostsFile ~/.ssh/guardian_known_hosts\n    HostKeyAlias n1\n"
	if got := renderFleetBlock("n1", "h.example.com", "ubuntu", 22, shapeRegistered); got != want {
		t.Errorf("registered block:\n%s", got)
	}
	mig := renderFleetBlock("n1", "100.64.0.5", "ubuntu", 22, shapeMigrated)
	if !strings.HasSuffix(mig, "    StrictHostKeyChecking accept-new\n") || strings.Contains(mig, "UserKnownHostsFile") {
		t.Errorf("migrated block:\n%s", mig)
	}
	for _, shape := range []fleetShape{shapeRegistered, shapeMigrated} {
		blk := parseFleetFile(renderFleetBlock("n1", "100.64.0.5", "ubuntu", 22, shape)).Blocks[0]
		if !blk.Managed || blk.Shape != shape {
			t.Errorf("shape %v does not parse back: %+v", shape, blk)
		}
	}
}

func TestDetectFleetInclude(t *testing.T) {
	home := "/home/ubuntu"
	fleet := "/home/ubuntu/.ssh/guardian_fleet.conf"
	cases := []struct {
		name, cfg string
		want      includeStatus
	}{
		{"tilde at top", "Include ~/.ssh/guardian_fleet.conf\nHost *\n  User x\n", includeStatus{true, true}},
		{"after comments", "# c\n\nInclude ~/.ssh/guardian_fleet.conf\n", includeStatus{true, true}},
		{"relative to ~/.ssh", "Include guardian_fleet.conf\n", includeStatus{true, true}},
		{"absolute", "Include /home/ubuntu/.ssh/guardian_fleet.conf\n", includeStatus{true, true}},
		{"quoted", "Include \"~/.ssh/guardian_fleet.conf\"\n", includeStatus{true, true}},
		{"equals form and case", "include=~/.ssh/guardian_fleet.conf\n", includeStatus{true, true}},
		{"glob", "Include ~/.ssh/guardian_*.conf\n", includeStatus{true, true}},
		{"several args", "Include /etc/ssh/a.conf ~/.ssh/guardian_fleet.conf\n", includeStatus{true, true}},
		{"after a Host line is not global", "Host a\n  User x\nInclude ~/.ssh/guardian_fleet.conf\n", includeStatus{true, false}},
		{"after a Match line", "Match all\nInclude ~/.ssh/guardian_fleet.conf\n", includeStatus{true, false}},
		{"other file", "Include ~/.ssh/other.conf\n", includeStatus{}},
		{"commented out", "# Include ~/.ssh/guardian_fleet.conf\n", includeStatus{}},
		{"empty", "", includeStatus{}},
		{"wrong dir", "Include /tmp/guardian_fleet.conf\n", includeStatus{}},
	}
	for _, c := range cases {
		if got := detectFleetInclude(c.cfg, fleet, home); got != c.want {
			t.Errorf("%s: got %+v, want %+v", c.name, got, c.want)
		}
	}
}

func TestWriteFileAtomicKeepsBackupAndMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "sub", "fleet.conf")
	if err := writeFileAtomic(path, []byte("one\n"), true); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path + ".bak"); !os.IsNotExist(err) {
		t.Error("no .bak expected for a first write")
	}
	if err := writeFileAtomic(path, []byte("two\n"), true); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(path); string(got) != "two\n" {
		t.Errorf("content = %q", got)
	}
	if got, _ := os.ReadFile(path + ".bak"); string(got) != "one\n" {
		t.Errorf(".bak = %q, want previous content", got)
	}
	for _, p := range []string{path, path + ".bak"} {
		if st, err := os.Stat(p); err != nil || st.Mode().Perm() != 0o600 {
			t.Errorf("%s mode = %v, %v; want 600", p, st.Mode().Perm(), err)
		}
	}
	entries, _ := os.ReadDir(filepath.Dir(path))
	for _, e := range entries {
		if strings.Contains(e.Name(), ".tmp-") {
			t.Errorf("temp file left behind: %s", e.Name())
		}
	}
}

func TestSnapshotRestore(t *testing.T) {
	dir := t.TempDir()
	existing := filepath.Join(dir, "a")
	missing := filepath.Join(dir, "b")
	_ = os.WriteFile(existing, []byte("orig"), 0o600)
	var tx txn
	if err := tx.track(existing, missing); err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(existing, []byte("changed"), 0o600)
	_ = os.WriteFile(missing, []byte("created"), 0o600)
	if err := tx.rollback(); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(existing); string(got) != "orig" {
		t.Errorf("existing = %q", got)
	}
	if _, err := os.Stat(missing); !os.IsNotExist(err) {
		t.Error("created file must be removed on rollback")
	}
}

func TestKnownHostsEditing(t *testing.T) {
	ed := hostKey{Type: "ssh-ed25519", Blob: testEd25519Blob}
	content := "other ssh-ed25519 AAAAother\n@cert-authority *.x ssh-rsa AAAAca\nn1 ssh-rsa OLDKEY\nn1 ssh-ed25519 OLDKEY2\nn10 ssh-ed25519 KEEP"
	got := knownHostsWith(content, "n1", ed)
	want := "other ssh-ed25519 AAAAother\n@cert-authority *.x ssh-rsa AAAAca\nn10 ssh-ed25519 KEEP\nn1 ssh-ed25519 " + testEd25519Blob + "\n"
	if got != want {
		t.Errorf("knownHostsWith:\n%q\nwant\n%q", got, want)
	}
	if out := knownHostsWithout(got, "n1"); strings.Contains(out, "n1 ssh") || !strings.Contains(out, "n10 ssh-ed25519 KEEP") {
		t.Errorf("knownHostsWithout:\n%q", out)
	}
	if knownHostsWith("", "a", ed) != "a ssh-ed25519 "+testEd25519Blob+"\n" {
		t.Error("empty file case")
	}
}

func TestValidateHostName(t *testing.T) {
	good := []string{"10.0.0.5", "203.0.113.9", "node1", "a.b-c.example.com", "Node-1.Example.COM", "x", "1abc.example.org", strings.Repeat("a", 63) + ".com"}
	for _, h := range good {
		if err := validateHostName(h); err != nil {
			t.Errorf("validateHostName(%q) = %v, want ok", h, err)
		}
	}
	bad := []string{
		"", "-oProxyCommand=id", "-host", "host name", "host\nProxyCommand id", "host\r", "host\n", "a b",
		"host;id", "host%h", "ho\"st", "ho'st", "host:22", "[::1]", "::1", "user@host", "host/../x", "$(id)", "`id`",
		"hôst.example.com", "хост.example.com", "host‮.com", "a..b", ".a.com", "a.com.", "-a.com", "a-.com",
		"999.1.1.1", "1.2.3", "010.0.0.1", "1.2.3.4.5", strings.Repeat("a", 64) + ".com", strings.Repeat("a.", 130) + "com", "host\x00",
	}
	for _, h := range bad {
		if err := validateHostName(h); err == nil {
			t.Errorf("validateHostName(%q) accepted", h)
		}
	}
}

func TestValidateIDUserPortFingerprint(t *testing.T) {
	for _, id := range []string{"a", "site-a-arm1", "edge-mini", "9lives", strings.Repeat("a", 63)} {
		if validateNodeID(id) != nil {
			t.Errorf("id %q rejected", id)
		}
	}
	for _, id := range []string{"", "-a", "A", "a_b", "a b", "a\n", "a.b", "ünï", "a/b", "../x", strings.Repeat("a", 64), "a$"} {
		if validateNodeID(id) == nil {
			t.Errorf("id %q accepted", id)
		}
	}
	for _, u := range []string{"ubuntu", "_svc", "deploy-1", "a_b"} {
		if validateUser(u) != nil {
			t.Errorf("user %q rejected", u)
		}
	}
	for _, u := range []string{"", "Root", "1abc", "-x", "a b", "a\n", "a;b", "ubuntu\nProxyCommand id", "ü", strings.Repeat("a", 33), "a@b", "%u"} {
		if validateUser(u) == nil {
			t.Errorf("user %q accepted", u)
		}
	}
	for _, p := range []int{1, 22, 2222, 65535} {
		if validatePort(p) != nil {
			t.Errorf("port %d rejected", p)
		}
	}
	for _, p := range []int{0, -1, 65536, 100000} {
		if validatePort(p) == nil {
			t.Errorf("port %d accepted", p)
		}
	}
	if validateFingerprint(testEd25519FP) != nil || validateFingerprint(testECDSAFP) != nil {
		t.Error("real fingerprints rejected")
	}
	for _, fp := range []string{"", "SHA256:short", testEd25519FP + "\n", "MD5:aa:bb", testEd25519FP + "x", "sha256:" + testEd25519FP[7:]} {
		if validateFingerprint(fp) == nil {
			t.Errorf("fingerprint %q accepted", fp)
		}
	}
}

func TestValidateMetadata(t *testing.T) {
	ok, err := validateMetadata(map[string]any{
		"tier": "Tier-2", "role": "ssh_target_node", "guardian_role": "node",
		"cli_support":    []any{"claude", "claude", "codex"},
		"tailscale_tags": []any{"tag:fleet"},
		"description":    "Edge box <ARM> & friends",
	}, false)
	if err != nil {
		t.Fatal(err)
	}
	if l := ok["cli_support"].([]string); len(l) != 2 {
		t.Errorf("cli_support not de-duplicated: %v", l)
	}
	if v, present := ok["role"]; !present || v != "ssh_target_node" {
		t.Errorf("role = %v", v)
	}
	roles, err := validateMetadata(map[string]any{"ops_roles": []any{"web", "llm_cli_host", "web"}}, false)
	if got, _ := roles["ops_roles"].([]string); err != nil || len(got) != 2 || got[1] != "llm_cli_host" {
		t.Errorf("ops_roles = %v, %v", roles["ops_roles"], err)
	}
	// Unset values come back as nil entries.
	unset, err := validateMetadata(map[string]any{"tier": nil, "role": "", "description": "  ", "cli_support": []any{}}, false)
	if err != nil || unset["tier"] != nil || unset["role"] != nil || unset["description"] != nil || unset["cli_support"] != nil || len(unset) != 4 {
		t.Errorf("unset = %v, %v", unset, err)
	}
	long := strings.Repeat("x", 501)
	manyTags := make([]any, 17)
	for i := range manyTags {
		manyTags[i] = "tag:t"
	}
	bad := []map[string]any{
		{"name": "x"},                                 // not settable
		{"arch": "x86_64"},                            // set by the probe, not typed
		{"tier": 3},                                   // wrong type
		{"tier": "Tier\n1"},                           // control character
		{"role": "has space"},                         // charset
		{"description": long},                         // length
		{"description": "line1\nline2"},               // control character
		{"cli_support": "claude"},                     // not a list
		{"cli_support": []any{"ok", 5}},               // non-string item
		{"cli_support": []any{"a b"}},                 // charset
		{"ops_roles": []any{"Web"}},                   // role names are lowercase
		{"ops_roles": []any{"web", "9db"}},            // must start with a letter
		{"ops_roles": "web"},                          // not a list
		{"tailscale_tags": []any{"fleet"}},            // missing tag: prefix
		{"tailscale_tags": manyTags},                  // too many
		{"guardian_role": "hub"},                      // cannot claim the hub role
		{"guardian_role": "node\nProxyCommand id"},    // injection attempt
		{"tailscale_tags": []any{"tag:a\nHost evil"}}, // injection attempt
	}
	for i, m := range bad {
		if _, err := validateMetadata(m, false); err == nil {
			t.Errorf("bad metadata %d accepted: %v", i, m)
		}
	}
	// Hub: role cannot change or be cleared, other fields can.
	if _, err := validateMetadata(map[string]any{"guardian_role": "node"}, true); err == nil {
		t.Error("hub guardian_role change accepted")
	}
	if _, err := validateMetadata(map[string]any{"guardian_role": nil}, true); err == nil {
		t.Error("hub guardian_role clear accepted")
	}
	if _, err := validateMetadata(map[string]any{"guardian_role": "hub", "tier": "Tier-1"}, true); err != nil {
		t.Errorf("hub keeping its role refused: %v", err)
	}
}

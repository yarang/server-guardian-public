package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const revokeTestBody = "AAAAC3NzaC1lZDI1NTE5AAAAIGd1YXJkaWFuLWh1Yi1mbGVldC1rZXktdGVzdDAwMDA="

func setRevokePub(t *testing.T, e *nodeEnv) string {
	t.Helper()
	pub := "ssh-ed25519 " + revokeTestBody + " guardian-hub-fleet@hub\n"
	if err := os.WriteFile(e.pub, []byte(pub), 0o600); err != nil {
		t.Fatal(err)
	}
	return "ssh-ed25519 " + revokeTestBody
}

func TestRevokePreview(t *testing.T) {
	e := newNodeEnv(t)
	match := setRevokePub(t, e)
	e.ssh.probe.stdout = ""
	rec, m := e.do("GET", "/api/nodes/legacy-1/revoke-key", nil)
	if rec.Code != 200 {
		t.Fatalf("code %d %s", rec.Code, rec.Body)
	}
	if m["node"] != "legacy-1" || m["target"] != "ubuntu@legacy.example.com" || m["match"] != match ||
		!strings.HasPrefix(m["fingerprint"].(string), "SHA256:") || m["note"] == "" {
		t.Fatalf("bad preview: %v", m)
	}
	if len(e.ssh.probed) != 0 {
		t.Fatalf("preview contacted the node: %v", e.ssh.probed)
	}
	if rec, _ := e.do("GET", "/api/nodes/nope/revoke-key", nil); rec.Code != 404 {
		t.Fatalf("unknown node: %d", rec.Code)
	}
}

func TestRevokeConfirmAndSSH(t *testing.T) {
	e := newNodeEnv(t)
	match := setRevokePub(t, e)
	if rec, _ := e.do("POST", "/api/nodes/legacy-1/revoke-key", map[string]string{"confirm_id": "other"}); rec.Code != 400 {
		t.Fatalf("mismatch: %d", rec.Code)
	}
	if rec, _ := e.do("POST", "/api/nodes/legacy-1/revoke-key", map[string]string{}); rec.Code != 400 {
		t.Fatalf("empty: %d", rec.Code)
	}
	if len(e.ssh.probed) != 0 {
		t.Fatal("ssh ran without confirmation")
	}

	e.ssh.probe.stdout = "removed=2\n"
	rec, m := e.do("POST", "/api/nodes/legacy-1/revoke-key", map[string]string{"confirm_id": "legacy-1"})
	if rec.Code != 200 || m["ok"] != true || m["removed"] != float64(2) {
		t.Fatalf("revoke: %d %v", rec.Code, m)
	}
	if len(e.ssh.probed) != 1 || !strings.HasPrefix(e.ssh.probed[0], "legacy-1 ") || !strings.Contains(e.ssh.probed[0], "k='"+match+"'") {
		t.Fatalf("unexpected ssh call: %v", e.ssh.probed)
	}

	e.ssh.probe.stdout = "removed=0\n"
	rec, m = e.do("POST", "/api/nodes/legacy-1/revoke-key", map[string]string{"confirm_id": "legacy-1"})
	if rec.Code != 200 || m["removed"] != float64(0) || m["note"] == nil {
		t.Fatalf("zero: %d %v", rec.Code, m)
	}

	e.ssh.probe.code, e.ssh.probe.stdout, e.ssh.probe.stderr = 255, "", "ssh: connect to host legacy.example.com port 22: Connection timed out"
	rec, m = e.do("POST", "/api/nodes/legacy-1/revoke-key", map[string]string{"confirm_id": "legacy-1"})
	if rec.Code != 502 || m["reachability"] != "unreachable" || m["error"] == nil {
		t.Fatalf("unreachable: %d %v", rec.Code, m)
	}
}

func TestRevokeRejectsUnsafeKey(t *testing.T) {
	e := newNodeEnv(t)
	bad := "ssh-ed25519 AAAA'; rm -rf ~; echo 'AAAAAAAAAAAAAAAAAAAA host\n"
	if err := os.WriteFile(e.pub, []byte(bad), 0o600); err != nil {
		t.Fatal(err)
	}
	rec, _ := e.do("POST", "/api/nodes/legacy-1/revoke-key", map[string]string{"confirm_id": "legacy-1"})
	if rec.Code != 409 || len(e.ssh.probed) != 0 {
		t.Fatalf("unsafe key accepted: %d %v", rec.Code, e.ssh.probed)
	}
}

// TestRevokeScriptRunsInSandbox executes the real script with sh against a temp HOME.
func TestRevokeScriptRunsInSandbox(t *testing.T) {
	home := t.TempDir()
	sshDir := filepath.Join(home, ".ssh")
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		t.Fatal(err)
	}
	ak := filepath.Join(sshDir, "authorized_keys")
	match := "ssh-ed25519 " + revokeTestBody
	content := "ssh-rsa AAAAOTHERKEY user@other\n" +
		`from="100.64.0.7",no-port-forwarding ` + match + " guardian-hub-fleet\n" +
		"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOTHER other\n"
	if err := os.WriteFile(ak, []byte(content), 0o640); err != nil {
		t.Fatal(err)
	}
	run := func() string {
		cmd := exec.Command("sh", "-c", fmt.Sprintf(revokeScript, match))
		cmd.Env = append(os.Environ(), "HOME="+home)
		out, err := cmd.Output()
		if err != nil {
			t.Fatalf("script: %v", err)
		}
		return string(out)
	}
	if out := run(); !strings.Contains(out, "removed=1") {
		t.Fatalf("out %q", out)
	}
	got, _ := os.ReadFile(ak)
	if strings.Contains(string(got), revokeTestBody) || !strings.Contains(string(got), "user@other") || !strings.Contains(string(got), "AAAAC3NzaC1lZDI1NTE5AAAAIOTHER") {
		t.Fatalf("wrong content: %q", got)
	}
	st, _ := os.Stat(ak)
	if st.Mode().Perm() != 0o640 {
		t.Fatalf("mode %v", st.Mode())
	}
	baks, _ := filepath.Glob(ak + ".guardian-bak-*")
	if len(baks) != 1 {
		t.Fatalf("backups: %v", baks)
	}
	if b, _ := os.ReadFile(baks[0]); string(b) != content {
		t.Fatal("backup differs from original")
	}
	if out := run(); !strings.Contains(out, "removed=0") {
		t.Fatalf("second run %q", out)
	}
}

func TestRevokeRefusesHubAndRecordsTimestamp(t *testing.T) {
	e := newNodeEnv(t)
	setRevokePub(t, e)
	for _, method := range []string{"GET", "POST"} {
		rec, m := e.do(method, "/api/nodes/hub-1/revoke-key", map[string]string{"confirm_id": "hub-1"})
		if rec.Code != 400 || m["error"] != "refusing to revoke the hub's key on the hub itself" {
			t.Fatalf("%s hub: %d %v", method, rec.Code, m)
		}
	}
	if len(e.ssh.probed) != 0 {
		t.Fatal("ssh ran for the hub")
	}
	if e.entry("legacy-1")["hub_key_revoked_at"] != nil {
		t.Fatal("timestamp set before revoke")
	}
	e.ssh.probe.stdout = "removed=1\n"
	rec, m := e.do("POST", "/api/nodes/legacy-1/revoke-key", map[string]string{"confirm_id": "legacy-1"})
	at, _ := m["hub_key_revoked_at"].(string)
	if rec.Code != 200 || at == "" {
		t.Fatalf("revoke: %d %v", rec.Code, m)
	}
	if _, err := time.Parse(time.RFC3339, at); err != nil {
		t.Fatal(err)
	}
	if e.entry("legacy-1")["hub_key_revoked_at"] != at {
		t.Fatalf("inventory entry: %v", e.entry("legacy-1"))
	}
	_, list := e.do("GET", "/api/nodes", nil)
	for _, s := range list["nodes"].([]any) {
		sm := s.(map[string]any)
		want := any(nil)
		if sm["id"] == "legacy-1" {
			want = at
		}
		if v, ok := sm["hub_key_revoked_at"]; !ok || v != want {
			t.Errorf("%v hub_key_revoked_at = %v (present %v), want %v", sm["id"], v, ok, want)
		}
	}
	// The revoke and a node write are in the audit log with the new fields.
	entries, _ := readAuditTail(50)
	var sawRevoke bool
	for _, a := range entries {
		if a.Route == "/api/nodes/{id}/revoke-key" && a.Status == "ok" && len(a.Targets) == 1 && a.Targets[0] == "legacy-1" && strings.Contains(a.Detail, "removed=1") {
			sawRevoke = true
		}
	}
	if !sawRevoke {
		t.Fatalf("revoke not audited: %+v", entries)
	}
}

package main

import (
	"os/exec"
	"strings"
	"testing"
)

const meshTestID = "hskey-authreq-AbCdEfGhIjKlMnOpQrSt_-12"

func meshEnv(t *testing.T) *nodeEnv {
	t.Helper()
	e := newNodeEnv(t)
	t.Setenv("GUARDIAN_HEADSCALE_HOST", "hs-1")
	t.Setenv("GUARDIAN_HEADSCALE_USER", "owner")
	t.Setenv("GUARDIAN_HEADSCALE_TAGS", "tag:fleet, tag:edge")
	return e
}

func TestMeshJoinOffWithoutConfiguration(t *testing.T) {
	e := newNodeEnv(t)
	t.Setenv("GUARDIAN_HEADSCALE_HOST", "")
	t.Setenv("GUARDIAN_HEADSCALE_USER", "")
	rec, m := e.do("GET", "/api/mesh/join", nil)
	if rec.Code != 200 || m["enabled"] != false {
		t.Fatalf("info: %d %v", rec.Code, m)
	}
	rec, m = e.do("POST", "/api/mesh/join", map[string]any{"auth": meshTestID, "tags": []string{"tag:fleet"}})
	if rec.Code != 409 || m["code"] != "mesh_join_off" || len(e.ssh.probed) != 0 {
		t.Fatalf("off: %d %v %v", rec.Code, m, e.ssh.probed)
	}
	t.Setenv("GUARDIAN_HEADSCALE_HOST", "hs-1")
	t.Setenv("GUARDIAN_HEADSCALE_USER", "owner; reboot")
	if _, m := e.do("GET", "/api/mesh/join", nil); m["enabled"] != false || m["problem"] == "" {
		t.Fatalf("bad user accepted: %v", m)
	}
}

func TestMeshJoinInfoReadsTheLoginServer(t *testing.T) {
	e := meshEnv(t)
	e.ssh.probe.stdout = "server_url=https://hs.example.test\n"
	rec, m := e.do("GET", "/api/mesh/join", nil)
	if rec.Code != 200 || m["ready"] != true || m["server_url"] != "https://hs.example.test" || m["host"] != "hs-1" {
		t.Fatalf("info: %d %v", rec.Code, m)
	}
	if tags := m["tags"].([]any); len(tags) != 2 || tags[1] != "tag:edge" {
		t.Fatalf("tags: %v", m["tags"])
	}
	e.ssh.probe.stdout = "error=the fleet user has no passwordless sudo on this host\n"
	e.ssh.probe.code = 3
	if _, m := e.do("GET", "/api/mesh/join", nil); m["ready"] != false || !strings.Contains(m["problem"].(string), "sudo") {
		t.Fatalf("not ready: %v", m)
	}
}

func TestMeshJoinValidatesBeforeSSH(t *testing.T) {
	e := meshEnv(t)
	for _, body := range []map[string]any{
		{"auth": "https://hs.example.test/register/nodekey:abc", "tags": []string{"tag:fleet"}},
		{"auth": meshTestID + " " + strings.Replace(meshTestID, "Ab", "Zz", 1), "tags": []string{"tag:fleet"}},
		{"auth": meshTestID + "x", "tags": []string{"tag:fleet"}},
		{"auth": meshTestID, "tags": []string{"tag:admin"}},
		{"auth": meshTestID, "tags": []string{}},
		{"auth": meshTestID, "tags": []string{"tag:fleet"}, "name": "Bad Name"},
		{"auth": meshTestID, "tags": []string{"tag:fleet"}, "user": "root"},
	} {
		if rec, m := e.do("POST", "/api/mesh/join", body); rec.Code != 400 {
			t.Errorf("%v accepted: %d %v", body, rec.Code, m)
		}
	}
	if len(e.ssh.probed) != 0 {
		t.Fatalf("ssh ran for an invalid request: %v", e.ssh.probed)
	}
}

func TestMeshJoinRegistersAndTags(t *testing.T) {
	e := meshEnv(t)
	e.ssh.probe.stdout = "node_id=29\nname=edge-7\nipv4=100.64.0.29\nipv6=fd7a:115c:a1e0::1d\ntags=tag:fleet\n"
	url := "https://hs.example.test/register/" + meshTestID
	rec, m := e.do("POST", "/api/mesh/join", map[string]any{"auth": "  " + url + "\n", "tags": []string{"tag:fleet", "tag:fleet"}, "name": "edge-7"})
	if rec.Code != 200 || m["ok"] != true || m["node_id"] != float64(29) || m["ipv4"] != "100.64.0.29" || m["name"] != "edge-7" {
		t.Fatalf("join: %d %v", rec.Code, m)
	}
	if len(e.ssh.probed) != 1 {
		t.Fatalf("calls: %v", e.ssh.probed)
	}
	script := e.ssh.probed[0]
	for _, want := range []string{"hs-1 ", "--auth-id '" + meshTestID + "' --user 'owner'", `nodes tag -i "$id" -t 'tag:fleet' >`, `nodes rename -i "$id" 'edge-7'`} {
		if !strings.Contains(script, want) {
			t.Errorf("script lacks %q:\n%s", want, script)
		}
	}
	if strings.Contains(script, "%!") {
		t.Errorf("format error in script:\n%s", script)
	}
	for _, s := range []string{strings.TrimPrefix(script, "hs-1 "), meshStatusScript} {
		check := exec.Command("sh", "-n")
		check.Stdin = strings.NewReader(s)
		if out, err := check.CombinedOutput(); err != nil {
			t.Errorf("sh -n: %v %s\n%s", err, out, s)
		}
	}

	e.ssh.probe.code, e.ssh.probe.stdout = 4, "error=registering node: auth request not found\n"
	rec, m = e.do("POST", "/api/mesh/join", map[string]any{"auth": meshTestID, "tags": []string{"tag:edge"}})
	if rec.Code != 502 || !strings.Contains(m["error"].(string), "not found") {
		t.Fatalf("failure: %d %v", rec.Code, m)
	}
}

func TestMeshFieldsKeepTheFirstValueAndJoinWarnings(t *testing.T) {
	f := meshFields("name=a\nwarning=x\nname=b\nwarning=y\nnoise\n")
	if f["name"] != "a" || f["warning"] != "x; y" {
		t.Fatalf("%v", f)
	}
}

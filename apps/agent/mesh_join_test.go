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

// The reserved tag:workstation marks an operator workstation (docs/design/operator-sessions.md, SD3a): it is
// usable only when GUARDIAN_HEADSCALE_TAGS lists it, it joins alone, and only its answer tells the operator to
// register the workstation on the hub. A server join answer carries neither key.
func TestMeshJoinWorkstationTag(t *testing.T) {
	cases := []struct {
		name        string
		allow       string   // GUARDIAN_HEADSCALE_TAGS
		tags        []string // the request's tags
		wantCode    int
		wantErr     string // refusal message fragment (400 cases)
		workstation bool   // the answer must be a workstation join (200 cases)
	}{
		{"listed alone", "tag:fleet, tag:edge, tag:workstation", []string{"tag:workstation"}, 200, "", true},
		{"listed twice", "tag:fleet, tag:edge, tag:workstation", []string{"tag:workstation", "tag:workstation"}, 200, "", true},
		{"with tag:fleet", "tag:fleet, tag:edge, tag:workstation", []string{"tag:workstation", "tag:fleet"}, 400, "alone", false},
		{"tag:fleet first", "tag:fleet, tag:edge, tag:workstation", []string{"tag:fleet", "tag:workstation"}, 400, "alone", false},
		{"with tag:edge", "tag:fleet, tag:edge, tag:workstation", []string{"tag:edge", "tag:workstation"}, 400, "alone", false},
		{"not listed", "tag:fleet, tag:edge", []string{"tag:workstation"}, 400, "not one of the tags", false},
		{"server join", "tag:fleet, tag:edge, tag:workstation", []string{"tag:fleet"}, 200, "", false},
	}
	for _, tc := range cases {
		e := meshEnv(t)
		t.Setenv("GUARDIAN_HEADSCALE_TAGS", tc.allow)
		e.ssh.probe.stdout = "node_id=31\nname=ws-mac-1\nipv4=100.64.0.31\nipv6=fd7a:115c:a1e0::1f\ntags=" + strings.Join(tc.tags, ",") + "\n"
		rec, m := e.do("POST", "/api/mesh/join", map[string]any{"auth": meshTestID, "tags": tc.tags, "name": "ws-mac-1"})
		if rec.Code != tc.wantCode {
			t.Errorf("%s: %d %v", tc.name, rec.Code, m)
			continue
		}
		if tc.wantCode == 400 {
			if m["field"] != "tags" || !strings.Contains(m["error"].(string), tc.wantErr) {
				t.Errorf("%s: refusal: %v", tc.name, m)
			}
			if len(e.ssh.probed) != 0 {
				t.Errorf("%s: ssh ran for a refused request: %v", tc.name, e.ssh.probed)
			}
			continue
		}
		if len(e.ssh.probed) != 1 {
			t.Fatalf("%s: calls: %v", tc.name, e.ssh.probed)
		}
		if !tc.workstation {
			if _, has := m["workstation"]; has {
				t.Errorf("%s: server join answer carries workstation: %v", tc.name, m)
			}
			if _, has := m["next"]; has {
				t.Errorf("%s: server join answer carries next: %v", tc.name, m)
			}
			continue
		}
		if m["workstation"] != true {
			t.Errorf("%s: workstation: %v", tc.name, m)
		}
		next, _ := m["next"].(string)
		if !strings.Contains(next, "guardian.py --session-add ws-mac-1 --address 100.64.0.31 --key '") {
			t.Errorf("%s: next = %q", tc.name, next)
		}
		if script := e.ssh.probed[0]; !strings.Contains(script, "-t 'tag:workstation'") {
			t.Errorf("%s: script lacks the workstation tag:\n%s", tc.name, script)
		}
	}
}

func TestMeshFieldsKeepTheFirstValueAndJoinWarnings(t *testing.T) {
	f := meshFields("name=a\nwarning=x\nname=b\nwarning=y\nnoise\n")
	if f["name"] != "a" || f["warning"] != "x; y" {
		t.Fatalf("%v", f)
	}
}

package main

import (
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
	"testing"
)

// meshNodeLine is one line of meshNodesScript's output.
func meshNodeLine(id int, name, ip string, tags []string, user string) string {
	b, _ := json.Marshal(map[string]any{"id": id, "name": name, "ipv4": ip, "ipv6": "", "tags": tags, "user": user,
		"online": true, "last_seen": 0})
	return "node=" + string(b) + "\n"
}

var meshListOut = meshNodeLine(1, "hs-1", "100.64.0.1", []string{"tag:mesh-ctl"}, "tagged-devices") +
	meshNodeLine(7, "hub-1", "100.64.0.7", []string{"tag:fleet", "tag:gitea"}, "tagged-devices") +
	meshNodeLine(12, "edge-7", "100.64.0.30", []string{"tag:fleet"}, "tagged-devices") +
	meshNodeLine(22, "laptop", "100.64.0.22", nil, "owner")

// meshStub answers the list script with list and the delete script with del, recording every script.
func meshStub(t *testing.T, list, del string) *[]string {
	t.Helper()
	var calls []string
	old := sshProbe
	sshProbe = func(host, cmd string, _ int) (int, string, string, int64) {
		calls = append(calls, host+" "+cmd)
		if strings.Contains(cmd, "nodes delete") {
			return 0, del, "", 1
		}
		return 0, list, "", 1
	}
	t.Cleanup(func() { sshProbe = old })
	return &calls
}

func TestMeshNodesMarksInventoryAndProtection(t *testing.T) {
	e := meshEnv(t)
	meshStub(t, meshListOut, "")
	rec, m := e.do("GET", "/api/mesh/nodes", nil)
	if rec.Code != 200 {
		t.Fatalf("list: %d %v", rec.Code, m)
	}
	got := map[string][2]string{}
	for _, raw := range m["nodes"].([]any) {
		n := raw.(map[string]any)
		inv, _ := n["inventory_id"].(string)
		prot, _ := n["protected"].(string)
		got[n["name"].(string)] = [2]string{inv, prot}
	}
	want := map[string][2]string{
		"hs-1": {"", "control_server"}, "hub-1": {"hub-1", "hub"}, "edge-7": {"", ""}, "laptop": {"", "unmanaged_tags"},
	}
	for name, w := range want {
		if got[name] != w {
			t.Errorf("%s: got %v, want %v", name, got[name], w)
		}
	}
}

func TestMeshNodeRemove(t *testing.T) {
	e := meshEnv(t)
	calls := meshStub(t, meshListOut, "removed=1\n")
	if rec, _ := e.do("POST", "/api/mesh/nodes/12/remove", map[string]string{"confirm_name": "edge-8"}); rec.Code != 400 {
		t.Fatalf("wrong name accepted: %d", rec.Code)
	}
	for _, c := range []struct {
		path, name string
		code       int
	}{
		{"/api/mesh/nodes/7/remove", "hub-1", 409},
		{"/api/mesh/nodes/1/remove", "hs-1", 409},
		{"/api/mesh/nodes/22/remove", "laptop", 409},
		{"/api/mesh/nodes/99/remove", "x", 404},
		{"/api/mesh/nodes/abc/remove", "x", 400},
	} {
		if rec, m := e.do("POST", c.path, map[string]string{"confirm_name": c.name}); rec.Code != c.code {
			t.Errorf("%s: %d %v", c.path, rec.Code, m)
		}
	}
	for _, call := range *calls {
		if strings.Contains(call, "nodes delete") {
			t.Fatalf("a refused request deleted a node: %s", call)
		}
	}
	rec, m := e.do("POST", "/api/mesh/nodes/12/remove", map[string]string{"confirm_name": "edge-7"})
	if rec.Code != 200 || m["ok"] != true || m["name"] != "edge-7" {
		t.Fatalf("remove: %d %v", rec.Code, m)
	}
	last := (*calls)[len(*calls)-1]
	if !strings.HasPrefix(last, "hs-1 ") || !strings.Contains(last, "nodes delete -i 12 --force") || strings.Contains(last, "%!") {
		t.Fatalf("delete script: %s", last)
	}
}

func TestMeshNodeRemoveReportsHeadscaleErrors(t *testing.T) {
	e := meshEnv(t)
	meshStub(t, meshListOut, "error=deleting node: not found\n")
	rec, m := e.do("POST", "/api/mesh/nodes/12/remove", map[string]string{"confirm_name": "edge-7"})
	if rec.Code != 502 || !strings.Contains(m["error"].(string), "not found") {
		t.Fatalf("failure: %d %v", rec.Code, m)
	}
	meshStub(t, "error=the fleet user has no passwordless sudo on this host\n", "")
	if rec, m := e.do("GET", "/api/mesh/nodes", nil); rec.Code != 502 || !strings.Contains(m["error"].(string), "sudo") {
		t.Fatalf("list failure: %d %v", rec.Code, m)
	}
}

func TestMeshNodeScriptsParse(t *testing.T) {
	for _, s := range []string{meshNodesScript, fmt.Sprintf(meshRemoveScript, 12)} {
		check := exec.Command("sh", "-n")
		check.Stdin = strings.NewReader(s)
		if out, err := check.CombinedOutput(); err != nil {
			t.Errorf("sh -n: %v %s\n%s", err, out, s)
		}
	}
}

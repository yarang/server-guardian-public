package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
)

// Tailnet node list and removal (docs/design/mesh-join.md, section 8). Removal deletes a node from headscale, so it
// leaves the tailnet; the hub refuses it for itself, the control server and every node that carries none of the tags
// this hub may give (people's devices and other roles are managed on the control server by hand).

// meshNodesScript prints one node=<json> line per node, summarised on the control server so node keys and pre-auth
// keys stay there.
const meshNodesScript = `set -u
command -v headscale >/dev/null 2>&1 || { echo 'error=headscale is not installed on this host'; exit 3; }
sudo -n true 2>/dev/null || { echo 'error=the fleet user has no passwordless sudo on this host'; exit 3; }
sudo -n headscale nodes list -o json 2>/dev/null | python3 -c '
import json, sys
for n in json.load(sys.stdin):
    ips = n.get("ip_addresses") or []
    print("node=" + json.dumps({
        "id": int(n.get("id", 0)), "name": n.get("given_name") or n.get("name") or "",
        "ipv4": next((a for a in ips if "." in a), ""), "ipv6": next((a for a in ips if ":" in a), ""),
        "tags": n.get("tags") or [], "user": (n.get("user") or {}).get("name") or "",
        "online": bool(n.get("online")), "last_seen": int((n.get("last_seen") or {}).get("seconds") or 0)}))
' || echo 'error=the node list could not be read'
`

// The id is formatted from an int, so the script holds no caller text.
const meshRemoveScript = `set -u
sudo -n true 2>/dev/null || { echo 'error=the fleet user has no passwordless sudo on this host'; exit 3; }
out=$(sudo -n headscale nodes delete -i %d --force 2>&1) || { echo "error=$(printf '%%s' "$out" | tr '\n' ' ' | tail -c 400)"; exit 4; }
echo removed=1
`

// meshNode is one tailnet node as the dashboard sees it.
type meshNode struct {
	ID       int      `json:"id"`
	Name     string   `json:"name"`
	IPv4     string   `json:"ipv4"`
	IPv6     string   `json:"ipv6"`
	Tags     []string `json:"tags"`
	User     string   `json:"user"`
	Online   bool     `json:"online"`
	LastSeen int64    `json:"last_seen"`
	// InventoryID is the Guardian node with this tailnet IPv4, when there is one.
	InventoryID string `json:"inventory_id,omitempty"`
	// Protected says why the dashboard may not remove the node: hub, control_server or unmanaged_tags.
	Protected string `json:"protected,omitempty"`
}

// meshNodes reads the node list from the control server and marks the inventory match and protection of each node.
func meshNodes(cfg *meshConfig) ([]meshNode, error) {
	code, stdout, stderr, _ := sshProbe(cfg.Host, meshNodesScript, meshTimeoutSec)
	var nodes []meshNode
	for _, line := range strings.Split(stdout, "\n") {
		if raw, ok := strings.CutPrefix(line, "node="); ok {
			var n meshNode
			if err := json.Unmarshal([]byte(raw), &n); err != nil {
				return nil, fmt.Errorf("unreadable node line from %s: %v", cfg.Host, err)
			}
			nodes = append(nodes, n)
		}
	}
	if f := meshFields(stdout); f["error"] != "" {
		return nil, errors.New(f["error"])
	}
	if code != 0 {
		return nil, fmt.Errorf("the hub could not read the node list on %s (%s)", cfg.Host, classifySSH(code, stderr))
	}
	inv, _ := loadNodes()
	byIP := map[string]string{}
	hubIP, controlIP := "", ""
	for _, s := range inv {
		if s.TailscaleIP == "" {
			continue
		}
		byIP[s.TailscaleIP] = s.ID
		if isHubNode(s) {
			hubIP = s.TailscaleIP
		}
		if s.ID == cfg.Host {
			controlIP = s.TailscaleIP
		}
	}
	managed := map[string]bool{}
	for _, t := range cfg.Tags {
		managed[t] = true
	}
	for i := range nodes {
		n := &nodes[i]
		if n.Tags == nil {
			n.Tags = []string{}
		}
		n.InventoryID = byIP[n.IPv4]
		switch {
		case n.IPv4 != "" && n.IPv4 == hubIP:
			n.Protected = "hub"
		case n.Name == cfg.Host || (n.IPv4 != "" && n.IPv4 == controlIP):
			n.Protected = "control_server"
		case !anyIn(n.Tags, managed):
			n.Protected = "unmanaged_tags"
		}
	}
	return nodes, nil
}

func anyIn(list []string, set map[string]bool) bool {
	for _, x := range list {
		if set[x] {
			return true
		}
	}
	return false
}

// GET /api/mesh/nodes
func handleMeshNodes(w http.ResponseWriter, r *http.Request) {
	cfg, problem := meshSettings()
	if cfg == nil {
		if problem == "" {
			problem = errMeshOff.Msg
		}
		writeAPIError(w, &apiError{Status: http.StatusConflict, Code: "mesh_join_off", Msg: problem})
		return
	}
	nodes, err := meshNodes(cfg)
	if err != nil {
		jsonResponse(w, http.StatusBadGateway, map[string]any{"error": err.Error(), "code": "mesh_nodes_failed"})
		return
	}
	jsonResponse(w, http.StatusOK, map[string]any{"host": cfg.Host, "tags": cfg.Tags, "nodes": nodes})
}

// @MX:WARN: [AUTO] Deletes a node from the tailnet control server.
// @MX:REASON: The node loses the tailnet (and the hub loses it); hub, control server and unmanaged nodes are refused.
func handleMeshNodeRemove(w http.ResponseWriter, r *http.Request) {
	a := beginAudit(r, "/api/mesh/nodes/{id}/remove")
	defer a.done()
	cfg, problem := meshSettings()
	if cfg == nil {
		if problem == "" {
			problem = errMeshOff.Msg
		}
		err := &apiError{Status: http.StatusConflict, Code: "mesh_join_off", Msg: problem}
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	a.targets = []string{cfg.Host}
	id, err := strconv.Atoi(r.PathValue("id"))
	if err != nil || id <= 0 || id > 1<<31 {
		err := badField("id", "must be a headscale node id (a positive number)")
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	var req struct {
		ConfirmName string `json:"confirm_name"`
	}
	if err := decodeNodeBody(w, r, &req); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	meshMu.Lock()
	defer meshMu.Unlock()
	// Read the list again: the decision rests on the control server's current state, not on the dashboard's copy.
	nodes, err := meshNodes(cfg)
	if err != nil {
		a.fail(err)
		jsonResponse(w, http.StatusBadGateway, map[string]any{"error": err.Error(), "code": "mesh_nodes_failed"})
		return
	}
	var node *meshNode
	for i := range nodes {
		if nodes[i].ID == id {
			node = &nodes[i]
		}
	}
	if node == nil {
		err := &apiError{Status: http.StatusNotFound, Code: "not_found", Msg: fmt.Sprintf("no tailnet node with id %d", id)}
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	if req.ConfirmName != node.Name {
		err := &apiError{Status: http.StatusBadRequest, Code: "confirm_mismatch", Field: "confirm_name",
			Msg: "confirm_name must equal the node name " + node.Name}
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	if node.Protected != "" {
		err := &apiError{Status: http.StatusConflict, Code: "mesh_node_protected",
			Msg: fmt.Sprintf("refusing to remove %s from the tailnet: %s", node.Name, meshProtectedText[node.Protected])}
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	code, stdout, stderr, _ := sshProbe(cfg.Host, fmt.Sprintf(meshRemoveScript, id), meshTimeoutSec)
	f := meshFields(stdout)
	if !strings.Contains(stdout, "removed=1") {
		msg := f["error"]
		if msg == "" {
			msg = "the hub could not run headscale on " + cfg.Host + " (" + classifySSH(code, stderr) + ")"
		}
		a.fail(errors.New("mesh remove failed: " + msg))
		jsonResponse(w, http.StatusBadGateway, map[string]any{"error": msg, "code": "mesh_remove_failed"})
		return
	}
	a.ok(fmt.Sprintf("mesh remove: node %d %s %s", id, node.Name, node.IPv4))
	resp := map[string]any{"ok": true, "id": id, "name": node.Name, "ipv4": node.IPv4}
	if node.InventoryID != "" {
		resp["inventory_id"] = node.InventoryID
		resp["note"] = "the hub can no longer reach " + node.InventoryID + " over the tailnet; remove it from Guardian as well"
	}
	jsonResponse(w, http.StatusOK, resp)
}

var meshProtectedText = map[string]string{
	"hub":            "it is the hub; the hub reaches the whole fleet over the tailnet",
	"control_server": "it is the headscale control server itself",
	"unmanaged_tags": "it carries none of the tags this hub manages (GUARDIAN_HEADSCALE_TAGS); remove it on the control server by hand",
}

package main

import (
	"errors"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
)

// Mesh join: the operator approves a new host's login to the headscale tailnet from the dashboard
// (docs/design/mesh-join.md). The new host runs `tailscale up --login-server <url>` itself and prints a
// registration URL; the operator pastes it here and the hub runs `headscale auth register` on the control server
// over its fleet SSH, then tags the node. No auth key exists in this flow.
//
// It is the only place where Guardian changes headscale, so it is off unless the hub's environment names the
// control server and the headscale user, and it can only give the tags listed in GUARDIAN_HEADSCALE_TAGS.

const (
	meshTimeoutSec  = 30
	meshDefaultTags = "tag:fleet"
	meshRoute       = "/api/mesh/join"

	// meshWorkstationTag is the reserved tag of an operator workstation (docs/design/operator-sessions.md, SD3a).
	// It is usable only when GUARDIAN_HEADSCALE_TAGS lists it, and a workstation joins with it alone: servers and
	// workstations get different ACLs, which the operator applies in headscale.
	meshWorkstationTag = "tag:workstation"
)

var (
	// headscale's AuthID: "hskey-authreq-" and 24 URL-safe random characters (hscontrol/types, v0.29).
	meshAuthIDRe = regexp.MustCompile(`hskey-authreq-[A-Za-z0-9_-]{24}`)
	meshUserRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$`)
	meshTagRe    = regexp.MustCompile(`^tag:[a-z0-9][a-z0-9-]{0,62}$`)
	meshResultRe = regexp.MustCompile(`(?m)^(error|warning|node_id|name|ipv4|ipv6|tags|server_url)=(.*)$`)
	meshMu       sync.Mutex
)

// meshConfig is the feature's configuration from the hub environment.
type meshConfig struct {
	Host string   // inventory id of the headscale server
	User string   // headscale user the nodes are registered under
	Tags []string // tags the dashboard may give a node
}

// meshSettings reads GUARDIAN_HEADSCALE_HOST, GUARDIAN_HEADSCALE_USER and GUARDIAN_HEADSCALE_TAGS. The first
// return is nil when the feature is off; the string says why (empty when it is simply not configured).
func meshSettings() (*meshConfig, string) {
	host := strings.TrimSpace(os.Getenv("GUARDIAN_HEADSCALE_HOST"))
	user := strings.TrimSpace(os.Getenv("GUARDIAN_HEADSCALE_USER"))
	if host == "" && user == "" {
		return nil, ""
	}
	if !validNodeID(host) {
		return nil, "GUARDIAN_HEADSCALE_HOST must be the inventory id of the headscale server"
	}
	if !meshUserRe.MatchString(user) {
		return nil, "GUARDIAN_HEADSCALE_USER must name the headscale user new nodes are registered under"
	}
	raw := os.Getenv("GUARDIAN_HEADSCALE_TAGS")
	if strings.TrimSpace(raw) == "" {
		raw = meshDefaultTags
	}
	var tags []string
	for _, t := range strings.Split(raw, ",") {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		if !meshTagRe.MatchString(t) {
			return nil, fmt.Sprintf("GUARDIAN_HEADSCALE_TAGS: %q is not a tag such as tag:fleet", t)
		}
		tags = append(tags, t)
	}
	if len(tags) == 0 {
		return nil, "GUARDIAN_HEADSCALE_TAGS lists no tag"
	}
	return &meshConfig{Host: host, User: user, Tags: tags}, ""
}

// meshAuthID finds exactly one registration id in what the operator pasted (the URL or the id itself).
func meshAuthID(text string) (string, error) {
	if len(text) > 1024 {
		return "", badField("auth", "paste the registration URL or id only")
	}
	found := map[string]bool{}
	for _, m := range meshAuthIDRe.FindAllString(text, -1) {
		found[m] = true
	}
	if len(found) != 1 {
		return "", badField("auth", "paste the registration URL that `tailscale up --login-server ...` printed on the new host (it contains hskey-authreq-...)")
	}
	for id := range found {
		// The id must not run on into more id characters (a longer, different string).
		i := strings.Index(text, id) + len(id)
		if i < len(text) && strings.ContainsRune("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", rune(text[i])) {
			return "", badField("auth", "the registration id is longer than headscale's 38 characters")
		}
		return id, nil
	}
	return "", nil
}

// Every substituted value is validated against a pattern without quotes, spaces or shell metacharacters
// (meshAuthIDRe, meshUserRe, meshTagRe, validNodeID). The node list summary runs on the control server, so node keys
// never leave it.
const meshStatusScript = `set -u
command -v headscale >/dev/null 2>&1 || { echo 'error=headscale is not installed on this host'; exit 3; }
sudo -n true 2>/dev/null || { echo 'error=the fleet user has no passwordless sudo on this host'; exit 3; }
echo "server_url=$(sed -n 's/^server_url: *//p' /etc/headscale/config.yaml 2>/dev/null | tr -d '"' | head -1)"
`

const meshRegisterScript = `set -u
command -v headscale >/dev/null 2>&1 || { echo 'error=headscale is not installed on this host'; exit 3; }
sudo -n true 2>/dev/null || { echo 'error=the fleet user has no passwordless sudo on this host'; exit 3; }
out=$(sudo -n headscale auth register --auth-id '%s' --user '%s' -o json 2>&1) || { echo "error=$(printf '%%s' "$out" | tr '\n' ' ' | tail -c 400)"; exit 4; }
id=$(printf '%%s' "$out" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["id"]))' 2>/dev/null) || { echo 'error=headscale registered the login but its answer had no node id; check the node list'; exit 5; }
echo "node_id=$id"
sudo -n headscale nodes tag -i "$id" %s >/dev/null 2>&1 || echo 'warning=the node was registered but tagging it failed; tag it on the control server'
%s
sudo -n headscale nodes list -o json 2>/dev/null | python3 -c '
import json, sys
want = int(sys.argv[1])
for n in json.load(sys.stdin):
    if int(n.get("id", 0)) == want:
        ips = n.get("ip_addresses") or []
        print("name=" + (n.get("given_name") or n.get("name") or ""))
        print("ipv4=" + next((a for a in ips if "." in a), ""))
        print("ipv6=" + next((a for a in ips if ":" in a), ""))
        print("tags=" + ",".join(n.get("tags") or []))
' "$id"
`

func meshScript(authID, user string, tags []string, name string) string {
	var flags []string
	for _, t := range tags {
		flags = append(flags, "-t '"+t+"'")
	}
	rename := ""
	if name != "" {
		rename = fmt.Sprintf(`sudo -n headscale nodes rename -i "$id" '%s' >/dev/null 2>&1 || echo 'warning=renaming the node failed'`, name)
	}
	return fmt.Sprintf(meshRegisterScript, authID, user, strings.Join(flags, " "), rename)
}

func meshFields(stdout string) map[string]string {
	out := map[string]string{}
	for _, m := range meshResultRe.FindAllStringSubmatch(stdout, -1) {
		key, value := m[1], strings.TrimSpace(m[2])
		switch {
		case key == "warning" && out[key] != "":
			out[key] += "; " + value
		case out[key] == "":
			out[key] = value
		}
	}
	return out
}

var errMeshOff = &apiError{Status: http.StatusConflict, Code: "mesh_join_off",
	Msg: "mesh join is off: set GUARDIAN_HEADSCALE_HOST and GUARDIAN_HEADSCALE_USER in the hub's agent.env"}

// GET /api/mesh/join: whether the feature is on, the control server, the user and tags, and the login server URL the
// new host must use (read from the control server, which also tells whether it is reachable with sudo).
func handleMeshJoinInfo(w http.ResponseWriter, r *http.Request) {
	cfg, problem := meshSettings()
	if cfg == nil {
		jsonResponse(w, http.StatusOK, map[string]any{"enabled": false, "problem": problem})
		return
	}
	resp := map[string]any{"enabled": true, "host": cfg.Host, "user": cfg.User, "tags": cfg.Tags, "ready": false}
	code, stdout, stderr, _ := sshProbe(cfg.Host, meshStatusScript, meshTimeoutSec)
	f := meshFields(stdout)
	switch {
	case f["error"] != "":
		resp["problem"] = f["error"]
	case code != 0:
		resp["problem"] = "the hub could not reach " + cfg.Host + " (" + classifySSH(code, stderr) + ")"
	case f["server_url"] == "":
		resp["problem"] = "server_url is missing from /etc/headscale/config.yaml on " + cfg.Host
	default:
		resp["ready"] = true
		resp["server_url"] = f["server_url"]
	}
	jsonResponse(w, http.StatusOK, resp)
}

// @MX:WARN: [AUTO] Writes to the tailnet control server: registers a node and gives it tags.
// @MX:REASON: The only change Guardian makes to headscale; inputs are pattern-validated, tags come from the hub env.
func handleMeshJoin(w http.ResponseWriter, r *http.Request) {
	cfg, problem := meshSettings()
	a := beginAudit(r, meshRoute)
	defer a.done()
	if cfg == nil {
		err := errMeshOff
		if problem != "" {
			err = &apiError{Status: http.StatusConflict, Code: "mesh_join_off", Msg: problem}
		}
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	a.targets = []string{cfg.Host}
	var req struct {
		Auth string   `json:"auth"`
		Tags []string `json:"tags"`
		Name string   `json:"name"`
	}
	if err := decodeNodeBody(w, r, &req); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	authID, err := meshAuthID(req.Auth)
	if err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	allowed := map[string]bool{}
	for _, t := range cfg.Tags {
		allowed[t] = true
	}
	tags := []string{}
	seen := map[string]bool{}
	for _, t := range req.Tags {
		if !allowed[t] {
			err := badField("tags", "%q is not one of the tags this hub may give: %s", t, strings.Join(cfg.Tags, ", "))
			a.fail(err)
			writeAPIError(w, err)
			return
		}
		if !seen[t] {
			seen[t] = true
			tags = append(tags, t)
		}
	}
	if len(tags) == 0 {
		err := badField("tags", "choose at least one tag; servers join the tailnet as tagged devices")
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	workstation := false
	for _, t := range tags {
		if t == meshWorkstationTag {
			workstation = true
		}
	}
	if workstation && len(tags) > 1 {
		err := badField("tags", "%s is the workstation tag: a workstation joins with it alone, never together with a server tag such as tag:fleet (servers and workstations get different ACLs, which the operator applies in headscale)", meshWorkstationTag)
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	name := strings.TrimSpace(req.Name)
	if name != "" && !validNodeID(name) {
		err := badField("name", "must match ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase letters, digits and dashes)")
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	meshMu.Lock()
	defer meshMu.Unlock()
	code, stdout, stderr, _ := sshProbe(cfg.Host, meshScript(authID, cfg.User, tags, name), meshTimeoutSec)
	f := meshFields(stdout)
	if f["node_id"] == "" {
		msg := f["error"]
		if msg == "" {
			msg = "the hub could not run headscale on " + cfg.Host + " (" + classifySSH(code, stderr) + ")"
		}
		if strings.Contains(msg, "registration cache") {
			msg += " — the login request expired or was already approved; run tailscale up on the new host again and paste the new URL"
		}
		a.fail(errors.New("mesh join failed: " + msg))
		jsonResponse(w, http.StatusBadGateway, map[string]any{"error": msg, "code": "mesh_join_failed"})
		return
	}
	nodeID, _ := strconv.Atoi(f["node_id"])
	resp := map[string]any{
		"ok": true, "node_id": nodeID, "name": f["name"], "ipv4": f["ipv4"], "ipv6": f["ipv6"],
		"tags": splitNonEmpty(f["tags"]), "user": cfg.User, "host": cfg.Host,
	}
	if f["warning"] != "" {
		resp["warning"] = f["warning"]
	}
	if workstation {
		// The next step lives on the hub (S1 of operator-sessions.md): register the workstation with its tailnet
		// address; the link public key comes from the workstation's installer.
		resp["workstation"] = true
		resp["next"] = fmt.Sprintf("guardian.py --session-add %s --address %s --key '<link public key>'", f["name"], f["ipv4"])
	}
	a.ok(fmt.Sprintf("mesh join: node %d %s %s tags=%s", nodeID, f["name"], f["ipv4"], strings.Join(tags, ",")))
	jsonResponse(w, http.StatusOK, resp)
}

func splitNonEmpty(s string) []string {
	out := []string{}
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func registerMeshRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET "+meshRoute, handleMeshJoinInfo)
	mux.HandleFunc("POST "+meshRoute, handleMeshJoin)
	mux.HandleFunc("GET /api/mesh/nodes", handleMeshNodes)
	mux.HandleFunc("POST /api/mesh/nodes/{id}/remove", handleMeshNodeRemove)
}

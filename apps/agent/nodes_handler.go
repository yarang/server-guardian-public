package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// Node registration API (docs/design/node-registration.md section 4). All routes sit behind the
// same withSecurity/withAuth chain as the rest of /api. Connection settings go to the fleet SSH
// file, node metadata to the hub's servers.json; both are written under fleetFileMu (plus an flock shared with scripts/setup-hub-ssh.sh) and put back
// when ssh does not resolve the node the way the request asked.

const (
	nodeBodyLimit    = 64 << 10
	keyscanTimeout   = 15 * time.Second
	probeTimeoutSec  = 10
	probeStderrLimit = 400
)

// apiError is an error with an HTTP status and a stable machine code for the UI.
type apiError struct {
	Status int
	Code   string
	Msg    string
	Field  string
	Hint   string
	Extra  map[string]any
}

func (e *apiError) Error() string { return e.Msg }

func writeAPIError(w http.ResponseWriter, err error) {
	var ae *apiError
	var fe *fieldError
	switch {
	case errors.As(err, &ae):
	case errors.As(err, &fe):
		ae = &apiError{Status: http.StatusBadRequest, Code: "invalid_field", Msg: fe.Error(), Field: fe.Field}
	default:
		ae = &apiError{Status: http.StatusInternalServerError, Code: "internal", Msg: err.Error()}
	}
	body := map[string]any{"error": ae.Msg, "code": ae.Code}
	if ae.Field != "" {
		body["field"] = ae.Field
	}
	if ae.Hint != "" {
		body["hint"] = ae.Hint
	}
	for k, v := range ae.Extra {
		body[k] = v
	}
	jsonResponse(w, ae.Status, body)
}

func decodeNodeBody(w http.ResponseWriter, r *http.Request, v any) error {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, nodeBodyLimit))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return &apiError{Status: http.StatusBadRequest, Code: "invalid_json", Msg: "Invalid JSON body: " + err.Error()}
	}
	if dec.More() {
		return &apiError{Status: http.StatusBadRequest, Code: "invalid_json", Msg: "Invalid JSON body: trailing data"}
	}
	return nil
}

func registerNodeRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/nodes/registration", handleNodeRegistration)
	mux.HandleFunc("POST /api/nodes/check", handleNodeCheck)
	mux.HandleFunc("POST /api/nodes", handleNodeRegister)
	mux.HandleFunc("PUT /api/nodes/{id}", handleNodeUpdate)
	mux.HandleFunc("DELETE /api/nodes/{id}", handleNodeDelete)
	mux.HandleFunc("POST /api/nodes/{id}/probe", handleNodeProbe)
	mux.HandleFunc("GET /api/nodes/{id}/revoke-key", handleRevokeKeyPreview)
	mux.HandleFunc("POST /api/nodes/{id}/revoke-key", handleRevokeKey)
}

// ---- shared pieces ----

// readFleet loads and parses the fleet file (a missing file is an empty one).
func readFleet() (*fleetFile, error) {
	data, _, err := readFileIfExists(fleetFilePath())
	if err != nil {
		return nil, fmt.Errorf("fleet file not readable: %w", err)
	}
	return parseFleetFile(string(data)), nil
}

func fleetInclude() includeStatus {
	data, _, _ := readFileIfExists(sshConfigPath())
	return detectFleetInclude(string(data), fleetFilePath(), sshHomeDir())
}

func requireInclude() error {
	if st := fleetInclude(); !st.Effective {
		msg := "~/.ssh/config does not include the fleet file before its first Host line"
		if st.Present {
			msg = "~/.ssh/config includes the fleet file only after a Host or Match line, where it does not apply to every host"
		}
		return &apiError{Status: http.StatusConflict, Code: "include_missing", Msg: msg,
			Hint: "add this line at the top of " + sshConfigPath() + ": " + fleetIncludeDirective()}
	}
	return nil
}

// scanHostKeys runs ssh-keyscan against a validated host and port.
func scanHostKeys(c connection) ([]hostKey, error) {
	// ssh-keyscan has no "--": validateHostName guarantees the host cannot start with "-".
	res := commandRunner("ssh-keyscan", []string{"-T", "5", "-p", strconv.Itoa(c.Port), c.HostName}, keyscanTimeout)
	if res.Err != nil {
		return nil, &apiError{Status: http.StatusBadGateway, Code: "scan_failed", Msg: "ssh-keyscan could not run: " + res.Err.Error()}
	}
	keys := parseKeyscan(res.Stdout)
	if len(keys) == 0 {
		return nil, &apiError{Status: http.StatusBadGateway, Code: "unreachable",
			Msg:  fmt.Sprintf("no SSH host key received from %s:%d", c.HostName, c.Port),
			Hint: tailText(res.Stderr, probeStderrLimit)}
	}
	return keys, nil
}

// confirmedKey rescans the host and returns the key whose fingerprint the caller confirmed.
func confirmedKey(c connection, fingerprint string) (hostKey, error) {
	if err := validateFingerprint(fingerprint); err != nil {
		return hostKey{}, err
	}
	keys, err := scanHostKeys(c)
	if err != nil {
		return hostKey{}, err
	}
	for _, k := range keys {
		if k.Fingerprint == fingerprint {
			return k, nil
		}
	}
	return hostKey{}, &apiError{Status: http.StatusConflict, Code: "fingerprint_mismatch",
		Msg: "the host presents different keys than the fingerprint you confirmed; check again", Field: "host_key_fingerprint",
		Extra: map[string]any{"keys": keys}}
}

// tailText keeps the last n bytes of s as one printable line.
func tailText(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) > n {
		s = s[len(s)-n:]
	}
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\r' || r == '\t' {
			return ' '
		}
		if unicode.IsControl(r) {
			return -1
		}
		return r
	}, s)
}

// txn remembers files so a failed operation can put every one of them back.
type txn struct{ snaps []fileSnapshot }

func (t *txn) track(paths ...string) error {
	for _, p := range paths {
		s, err := snapshotFile(p)
		if err != nil {
			return err
		}
		t.snaps = append(t.snaps, s)
	}
	return nil
}

func (t *txn) rollback() error {
	var errs []error
	for i := len(t.snaps) - 1; i >= 0; i-- {
		if err := t.snaps[i].restore(); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// failRolledBack restores the snapshots and builds the error to return.
func (t *txn) failRolledBack(code, msg string) error {
	if err := t.rollback(); err != nil {
		return &apiError{Status: http.StatusInternalServerError, Code: code, Msg: msg + "; rollback also failed: " + err.Error()}
	}
	return &apiError{Status: http.StatusInternalServerError, Code: code, Msg: msg + "; changes were rolled back"}
}

// probeCommand prints the architecture, then whether the remote user has passwordless sudo: the ops cleanup
// actions (docs/design/server-ops-project.md) need `sudo -n` for journal, apt and kernel work.
const probeCommand = "uname -m; if sudo -n true 2>/dev/null; then echo sudo=yes; else echo sudo=no; fi"

// probeResult is the outcome of `ssh <id> <probeCommand>`.
type probeResult struct {
	// Status is ok, key_not_installed (Permission denied) or unreachable.
	Status string `json:"status"`
	// Reachability is classifySSH of the probe (ok, auth_failed, host_key_changed, unreachable,
	// command_failed); Status keeps the older three-value vocabulary.
	Reachability string `json:"reachability"`
	Arch         string `json:"arch,omitempty"`
	// Sudo is "yes" or "no" for passwordless sudo of the remote user; absent when the probe did not say.
	Sudo       string `json:"sudo,omitempty"`
	Stderr     string `json:"stderr,omitempty"`
	DurationMs int64  `json:"duration_ms"`
}

func probeNode(id string) probeResult {
	code, stdout, stderr, dur := sshProbe(id, probeCommand, probeTimeoutSec)
	res := probeResult{DurationMs: dur, Reachability: classifySSH(code, stderr)}
	switch {
	case code == 0:
		res.Status = "ok"
		if arch := strings.TrimSpace(strings.SplitN(stdout, "\n", 2)[0]); archRe.MatchString(arch) {
			res.Arch = arch
		}
		for _, line := range strings.Split(stdout, "\n") {
			if v, ok := strings.CutPrefix(strings.TrimSpace(line), "sudo="); ok && (v == "yes" || v == "no") {
				res.Sudo = v
			}
		}
	case res.Reachability == reachAuthFailed:
		res.Status = "key_not_installed"
		res.Stderr = tailText(stderr, probeStderrLimit)
	default:
		res.Status = "unreachable"
		res.Stderr = tailText(stderr, probeStderrLimit)
	}
	return res
}

// storeArch records a probed architecture in the inventory entry.
func storeArch(id, arch string) error {
	if arch == "" {
		return nil
	}
	fleetFileMu.Lock()
	defer fleetFileMu.Unlock()
	path, err := inventoryWritePath()
	if err != nil {
		return err
	}
	inv, err := loadInventory(path)
	if err != nil {
		return err
	}
	i := inv.index(id)
	if i < 0 {
		return fmt.Errorf("%s is no longer in the inventory", id)
	}
	obj, err := inv.object(i)
	if err != nil {
		return err
	}
	if obj.str("arch") == arch {
		return nil
	}
	if err := obj.set("arch", arch); err != nil {
		return err
	}
	inv.replace(i, obj)
	return inv.save()
}

// nodeViewOf builds the /api/nodes shape of one inventory entry, fleet file overlay included.
func nodeViewOf(raw json.RawMessage, ff *fleetFile) (FleetNode, error) {
	var n FleetNode
	if err := json.Unmarshal(raw, &n); err != nil {
		return n, err
	}
	one := []FleetNode{n}
	overlayConnection(one, ff)
	return one[0], nil
}

// overlayConnection copies HostName/User/Port from managed fleet blocks onto the nodes and
// labels where each node's connection settings come from.
func overlayConnection(nodes []FleetNode, ff *fleetFile) {
	managed := ff.managedByAlias()
	for i := range nodes {
		if b, ok := managed[nodes[i].ID]; ok {
			nodes[i].Hostname, nodes[i].User, nodes[i].Port = b.HostName, b.User, b.Port
			nodes[i].Connection = "fleet_file"
			nodes[i].HostKeyMode = "accept-new"
			if b.Shape == shapeRegistered {
				nodes[i].HostKeyMode = "pinned"
			}
		} else {
			nodes[i].Connection = "inventory"
			nodes[i].HostKeyMode = ""
		}
	}
}

// loadNodesWithConnection is loadNodes plus the fleet file overlay, for /api/nodes.
func loadNodesWithConnection() ([]FleetNode, error) {
	nodes, err := loadNodes()
	if err != nil {
		return nil, err
	}
	ff, err := readFleet()
	if err != nil {
		ff = &fleetFile{}
	}
	overlayConnection(nodes, ff)
	return nodes, nil
}

// authorizedKeyLine is the authorized_keys line to install on a node: the hub's fleet public key,
// restricted with from="<hub tailscale ip>" like scripts/setup-hub-ssh.sh does. When the hub's
// address is unknown it returns no line and a blocker message instead of an unrestricted key that
// would work from any source address; the same holds when the public key itself is unusable.
func authorizedKeyLine(hubIP string) (line, problem string) {
	data, exists, err := readFileIfExists(fleetPubKeyPath())
	if err != nil || !exists {
		return "", "the hub fleet public key " + fleetPubKeyPath() + " is missing; run scripts/setup-hub-ssh.sh"
	}
	fields := strings.Fields(strings.SplitN(string(data), "\n", 2)[0])
	if len(fields) < 2 || !strings.HasPrefix(fields[0], "ssh-") {
		return "", "the hub fleet public key " + fleetPubKeyPath() + " is not a public key line"
	}
	comment := "guardian-hub-fleet"
	if len(fields) > 2 && strings.Contains(strings.Join(fields[2:], " "), "guardian-hub-fleet") {
		comment = strings.Join(fields[2:], " ")
	}
	if !isIPv4Literal(hubIP) {
		return "", "the hub has no tailscale_ip in the inventory, so no from=\"...\" restricted key line can be built; set tailscale_ip on the hub entry"
	}
	opts := `from="` + hubIP + `",no-agent-forwarding,no-port-forwarding,no-X11-forwarding`
	return opts + " " + fields[0] + " " + fields[1] + " " + comment, ""
}

// inventoryConnection is the connection an inventory-only node would get: the Tailscale address
// when it is in 100.64.x (as setup-hub-ssh.sh does), else hostname; user defaults to ubuntu and
// port to 22. The values are validated by the caller.
func inventoryConnection(obj *jsonObject) connection {
	c := connection{HostName: obj.str("hostname"), User: obj.str("user"), Port: 22}
	if ip := obj.str("tailscale_ip"); strings.HasPrefix(ip, "100.64.") {
		c.HostName = ip
	}
	if c.User == "" {
		c.User = "ubuntu"
	}
	if raw, ok := obj.vals["port"]; ok {
		var p int
		if json.Unmarshal(raw, &p) == nil && p != 0 {
			c.Port = p
		}
	}
	return c
}

func hubTailscaleIP(inv *inventory) string {
	if inv == nil {
		return ""
	}
	for i := range inv.entries {
		if o, err := inv.object(i); err == nil && o.str("guardian_role") == "hub" {
			return o.str("tailscale_ip")
		}
	}
	return ""
}

// ---- GET /api/nodes/registration ----

func handleNodeRegistration(w http.ResponseWriter, r *http.Request) {
	st := fleetInclude()
	resp := map[string]any{
		"fleet_file":        fleetFilePath(),
		"known_hosts_file":  knownHostsPath(),
		"ssh_config_file":   sshConfigPath(),
		"include_line":      fleetIncludeDirective(),
		"include_present":   st.Present,
		"include_effective": st.Effective,
	}
	var blockers []string
	if !st.Effective {
		blockers = append(blockers, "add \""+fleetIncludeDirective()+"\" at the top of "+sshConfigPath())
	}
	var inv *inventory
	invPath, err := inventoryWritePath()
	resp["inventory_file"] = invPath
	resp["inventory_writable"] = err == nil
	if err != nil {
		blockers = append(blockers, err.Error())
		if p, perr := serversFile(); perr == nil {
			resp["inventory_file"] = p
			inv, _ = loadInventory(p)
		}
	} else {
		inv, _ = loadInventory(invPath)
	}
	hubIP := hubTailscaleIP(inv)
	resp["hub_tailscale_ip"] = hubIP
	line, problem := authorizedKeyLine(hubIP)
	resp["fleet_public_key"] = line
	resp["validation"] = nodeFormRules()
	if problem != "" {
		resp["fleet_public_key_note"] = problem
		resp["fleet_public_key_blocker"] = problem
	}
	ff, ferr := readFleet()
	managed, unmanaged := []string{}, []string{}
	if ferr == nil {
		for _, b := range ff.Blocks {
			if b.Managed {
				managed = append(managed, b.Alias)
			}
		}
		unmanaged = append(unmanaged, ff.unmanagedAliases()...)
	} else {
		blockers = append(blockers, ferr.Error())
	}
	resp["managed_nodes"] = managed
	resp["unmanaged_hosts"] = unmanaged
	if blockers == nil {
		blockers = []string{}
	}
	resp["blockers"] = blockers
	resp["can_register"] = len(blockers) == 0
	jsonResponse(w, http.StatusOK, resp)
}

// ---- POST /api/nodes/check ----

type nodeCheckRequest struct {
	ID       string `json:"id"`
	Hostname string `json:"hostname"`
	User     string `json:"user"`
	Port     *int   `json:"port"`
}

func (q nodeCheckRequest) connection() connection {
	port := 22
	if q.Port != nil {
		port = *q.Port
	}
	return connection{HostName: q.Hostname, User: q.User, Port: port}
}

func handleNodeCheck(w http.ResponseWriter, r *http.Request) {
	var req nodeCheckRequest
	if err := decodeNodeBody(w, r, &req); err != nil {
		writeAPIError(w, err)
		return
	}
	if err := validateNodeID(req.ID); err != nil {
		writeAPIError(w, err)
		return
	}
	c := req.connection()
	if err := validateConnection(c); err != nil {
		writeAPIError(w, err)
		return
	}
	keys, err := scanHostKeys(c)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	exists := false
	if nodes, err := loadNodes(); err == nil {
		for _, s := range nodes {
			exists = exists || s.ID == req.ID || s.Name == req.ID
		}
	}
	if ff, err := readFleet(); err == nil {
		if b, _ := ff.find(req.ID); b != nil {
			exists = true
		}
	}
	jsonResponse(w, http.StatusOK, map[string]any{
		"ok": true, "id": req.ID, "hostname": c.HostName, "port": c.Port,
		"keys": keys, "id_exists": exists,
	})
}

// ---- POST /api/nodes ----

type nodeRegisterRequest struct {
	ID                 string         `json:"id"`
	Hostname           string         `json:"hostname"`
	User               string         `json:"user"`
	Port               *int           `json:"port"`
	HostKeyFingerprint string         `json:"host_key_fingerprint"`
	Metadata           map[string]any `json:"metadata"`
}

func handleNodeRegister(w http.ResponseWriter, r *http.Request) {
	a := beginAudit(r, "/api/nodes")
	defer a.done()
	var req nodeRegisterRequest
	if err := decodeNodeBody(w, r, &req); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	a.targets = []string{req.ID}
	c := nodeCheckRequest{Hostname: req.Hostname, User: req.User, Port: req.Port}.connection()
	if err := validateNodeID(req.ID); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	if err := validateConnection(c); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	if req.HostKeyFingerprint == "" {
		a.fail(errors.New("host_key_fingerprint is required"))
		writeAPIError(w, &apiError{Status: http.StatusBadRequest, Code: "fingerprint_required", Field: "host_key_fingerprint",
			Msg: "host_key_fingerprint is required: run the check first and confirm the host key"})
		return
	}
	if err := validateFingerprint(req.HostKeyFingerprint); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	meta, err := validateMetadata(req.Metadata, false)
	if err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}

	view, ierr := registerNode(req, c, meta)
	if ierr != nil {
		a.fail(ierr)
		writeAPIError(w, ierr)
		return
	}
	a.ok("register node " + req.ID + " host=" + c.HostName + " user=" + c.User + " port=" + strconv.Itoa(c.Port))
	resp := map[string]any{"ok": true, "node": view}
	resp = addProbe(resp, req.ID)
	jsonResponse(w, http.StatusCreated, resp)
}

// addProbe probes the node, stores its architecture and adds the result to resp. A node that
// answers with Permission denied also gets the authorized_keys line to install.
func addProbe(resp map[string]any, id string) map[string]any {
	p := probeNode(id)
	resp["probe"] = p
	var warnings []string
	if p.Status == "ok" {
		if err := storeArch(id, p.Arch); err != nil {
			warnings = append(warnings, "could not store arch: "+err.Error())
		} else if n, ok := resp["node"].(FleetNode); ok && p.Arch != "" {
			n.Arch = p.Arch
			resp["node"] = n
		}
	} else if p.Status == "key_not_installed" {
		var inv *inventory
		if path, err := serversFile(); err == nil {
			inv, _ = loadInventory(path)
		}
		line, blocker := authorizedKeyLine(hubTailscaleIP(inv))
		resp["authorized_key"] = line
		if line == "" {
			resp["authorized_key_blocker"] = blocker
		}
	}
	if len(warnings) > 0 {
		resp["warnings"] = warnings
	}
	return resp
}

// @MX:WARN: [AUTO] Writes the hub's fleet SSH file, pinned host keys and node inventory from a dashboard request.
// @MX:REASON: Only validated hostname/user/port reach the fleet file (fixed block shape, no ProxyCommand or Match exec); the result is checked with ssh -G and rolled back on mismatch.
func registerNode(req nodeRegisterRequest, c connection, meta map[string]any) (FleetNode, error) {
	unlock, lerr := lockFleet()
	if lerr != nil {
		return FleetNode{}, lerr
	}
	defer unlock()

	invPath, err := inventoryWritePath()
	if err != nil {
		return FleetNode{}, err
	}
	if err := requireInclude(); err != nil {
		return FleetNode{}, err
	}
	inv, err := loadInventory(invPath)
	if err != nil {
		return FleetNode{}, err
	}
	ff, err := readFleet()
	if err != nil {
		return FleetNode{}, err
	}
	if inv.taken(req.ID) {
		return FleetNode{}, &apiError{Status: http.StatusConflict, Code: "id_exists", Field: "id", Msg: "a node with this id or name already exists in the inventory"}
	}
	if b, managed := ff.find(req.ID); b != nil {
		if managed {
			return FleetNode{}, &apiError{Status: http.StatusConflict, Code: "id_exists", Field: "id",
				Msg: "the fleet file already has a managed Host block for " + req.ID}
		}
		return FleetNode{}, unmanagedConflict(req.ID)
	}

	key, err := confirmedKey(c, req.HostKeyFingerprint)
	if err != nil {
		return FleetNode{}, err
	}

	entry := &jsonObject{vals: map[string]json.RawMessage{}}
	for _, kv := range []struct {
		k string
		v any
	}{{"id", req.ID}, {"name", req.ID}} {
		_ = entry.set(kv.k, kv.v)
	}
	// Nodes run no agent (only the hub does), so the default role is "node".
	if _, ok := meta["guardian_role"]; !ok {
		meta["guardian_role"] = "node"
	}
	for _, k := range metadataKeys {
		if v := meta[k]; v != nil {
			if err := entry.set(k, v); err != nil {
				return FleetNode{}, err
			}
		}
	}

	known, _, err := readFileIfExists(knownHostsPath())
	if err != nil {
		return FleetNode{}, err
	}
	var t txn
	if err := t.track(knownHostsPath(), fleetFilePath(), invPath); err != nil {
		return FleetNode{}, err
	}
	ff.set(req.ID, c.HostName, c.User, c.Port, shapeRegistered)
	inv.append(entry)
	werr := writeFileAtomic(knownHostsPath(), []byte(knownHostsWith(string(known), req.ID, key)), true)
	if werr == nil {
		werr = writeFileAtomic(fleetFilePath(), []byte(ff.render()), true)
	}
	if werr == nil {
		werr = inv.save()
	}
	if werr != nil {
		return FleetNode{}, t.failRolledBack("write_failed", "writing the registration failed: "+werr.Error())
	}
	if verr := verifyEffective(req.ID, c.HostName, c.User, c.Port, shapeRegistered); verr != nil {
		return FleetNode{}, t.failRolledBack("verify_failed", verr.Error())
	}
	return nodeViewOf(entry.raw(), ff)
}

func unmanagedConflict(id string) error {
	return &apiError{Status: http.StatusConflict, Code: "unmanaged_block", Field: "id",
		Msg:  "the fleet file has a Host block for " + id + " that was edited by hand or has another shape; the agent will not change it",
		Hint: "edit or remove that block in " + fleetFilePath() + " yourself"}
}

// ---- PUT /api/nodes/{id} ----

type nodeUpdateRequest struct {
	Hostname           *string        `json:"hostname"`
	User               *string        `json:"user"`
	Port               *int           `json:"port"`
	HostKeyFingerprint string         `json:"host_key_fingerprint"`
	Metadata           map[string]any `json:"metadata"`
}

func handleNodeUpdate(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	a := beginAudit(r, "/api/nodes/{id}", id)
	defer a.done()
	if err := validateNodeID(id); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	var req nodeUpdateRequest
	if err := decodeNodeBody(w, r, &req); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	view, connWritten, err := updateNode(id, req)
	if err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	a.ok("update node " + id + ": " + updateSummary(req))
	resp := map[string]any{"ok": true, "node": view}
	if connWritten {
		resp = addProbe(resp, id)
	}
	jsonResponse(w, http.StatusOK, resp)
}

// @MX:WARN: [AUTO] Rewrites a node's fleet block, pinned host key and inventory entry from a dashboard request.
// @MX:REASON: Same boundary as registerNode: validated inputs only, a changed hostname or port needs a freshly confirmed host key, ssh -G verifies, failures roll back.
func updateNode(id string, req nodeUpdateRequest) (FleetNode, bool, error) {
	unlock, lerr := lockFleet()
	if lerr != nil {
		return FleetNode{}, false, lerr
	}
	defer unlock()

	invPath, err := inventoryWritePath()
	if err != nil {
		return FleetNode{}, false, err
	}
	inv, err := loadInventory(invPath)
	if err != nil {
		return FleetNode{}, false, err
	}
	ff, err := readFleet()
	if err != nil {
		return FleetNode{}, false, err
	}
	idx := inv.index(id)
	block, managed := ff.find(id)
	if idx < 0 {
		return FleetNode{}, false, &apiError{Status: http.StatusNotFound, Code: "not_found", Msg: "unknown node " + id}
	}
	obj, err := inv.object(idx)
	if err != nil {
		return FleetNode{}, false, err
	}
	meta, err := validateMetadata(req.Metadata, obj.str("guardian_role") == "hub")
	if err != nil {
		return FleetNode{}, false, err
	}

	// A bare host_key_fingerprint re-confirms the host key (pins an accept-new or inventory-only node).
	connRequested := req.Hostname != nil || req.User != nil || req.Port != nil || req.HostKeyFingerprint != ""
	var t txn
	var newConn connection
	connWritten := false
	var newKey *hostKey
	shape := shapeMigrated
	if connRequested {
		if block != nil && !managed {
			return FleetNode{}, false, unmanagedConflict(id)
		}
		cur := inventoryConnection(obj)
		if managed {
			cur = connection{HostName: block.HostName, User: block.User, Port: block.Port}
			shape = block.Shape
		}
		newConn = cur
		if req.Hostname != nil {
			newConn.HostName = *req.Hostname
		}
		if req.User != nil {
			newConn.User = *req.User
		}
		if req.Port != nil {
			newConn.Port = *req.Port
		}
		if err := validateConnection(newConn); err != nil {
			return FleetNode{}, false, err
		}
		if err := requireInclude(); err != nil {
			return FleetNode{}, false, err
		}
		changed := !managed || newConn != cur || req.HostKeyFingerprint != ""
		if changed {
			// A new destination, or an explicit re-confirmation, needs a host key the operator has just confirmed.
			needsKey := !managed || newConn.HostName != cur.HostName || newConn.Port != cur.Port || req.HostKeyFingerprint != ""
			if needsKey {
				if req.HostKeyFingerprint == "" {
					return FleetNode{}, false, &apiError{Status: http.StatusBadRequest, Code: "fingerprint_required", Field: "host_key_fingerprint",
						Msg: "changing the hostname or port needs host_key_fingerprint: run the check and confirm the new host key"}
				}
				k, err := confirmedKey(newConn, req.HostKeyFingerprint)
				if err != nil {
					return FleetNode{}, false, err
				}
				newKey = &k
				shape = shapeRegistered
			}
			connWritten = true
		}
	}

	if connWritten {
		known, _, err := readFileIfExists(knownHostsPath())
		if err != nil {
			return FleetNode{}, false, err
		}
		if err := t.track(knownHostsPath(), fleetFilePath()); err != nil {
			return FleetNode{}, false, err
		}
		ff.set(id, newConn.HostName, newConn.User, newConn.Port, shape)
		var werr error
		if newKey != nil {
			werr = writeFileAtomic(knownHostsPath(), []byte(knownHostsWith(string(known), id, *newKey)), true)
		}
		if werr == nil {
			werr = writeFileAtomic(fleetFilePath(), []byte(ff.render()), true)
		}
		if werr != nil {
			return FleetNode{}, false, t.failRolledBack("write_failed", "writing the fleet file failed: "+werr.Error())
		}
	}
	metaChanged := false
	for _, k := range metadataKeys {
		v, ok := meta[k]
		if !ok {
			continue
		}
		metaChanged = true
		if v == nil {
			obj.del(k)
		} else if err := obj.set(k, v); err != nil {
			_ = t.rollback()
			return FleetNode{}, false, err
		}
	}
	if metaChanged {
		if err := t.track(invPath); err != nil {
			_ = t.rollback()
			return FleetNode{}, false, err
		}
		inv.replace(idx, obj)
		if err := inv.save(); err != nil {
			return FleetNode{}, false, t.failRolledBack("write_failed", "writing the inventory failed: "+err.Error())
		}
	}
	if connWritten {
		if verr := verifyEffective(id, newConn.HostName, newConn.User, newConn.Port, shape); verr != nil {
			return FleetNode{}, false, t.failRolledBack("verify_failed", verr.Error())
		}
	}
	view, err := nodeViewOf(inv.entries[idx], ff)
	return view, connWritten, err
}

// updateSummary lists which parts of an update request were present (values are not logged).
func updateSummary(req nodeUpdateRequest) string {
	var parts []string
	if req.Hostname != nil {
		parts = append(parts, "hostname")
	}
	if req.User != nil {
		parts = append(parts, "user")
	}
	if req.Port != nil {
		parts = append(parts, "port")
	}
	if len(req.Metadata) > 0 {
		parts = append(parts, "metadata")
	}
	if len(parts) == 0 {
		return "no fields"
	}
	return strings.Join(parts, ",")
}

// ---- DELETE /api/nodes/{id} ----

func handleNodeDelete(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	a := beginAudit(r, "/api/nodes/{id}", id)
	defer a.done()
	if err := validateNodeID(id); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	removed, err := deleteNode(id)
	if err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	a.ok(fmt.Sprintf("delete node %s: fleet_block=%t known_hosts=%t inventory=%t", id, removed["fleet_block"], removed["known_hosts"], removed["inventory"]))
	jsonResponse(w, http.StatusOK, map[string]any{"ok": true, "id": id, "removed": removed})
}

// @MX:WARN: [AUTO] Removes a node's fleet block, pinned host keys and inventory entry.
// @MX:REASON: The hub entry is refused and hand-edited blocks are never touched; all three files are restored if any write fails.
func deleteNode(id string) (map[string]bool, error) {
	unlock, lerr := lockFleet()
	if lerr != nil {
		return nil, lerr
	}
	defer unlock()

	invPath, err := inventoryWritePath()
	if err != nil {
		return nil, err
	}
	inv, err := loadInventory(invPath)
	if err != nil {
		return nil, err
	}
	ff, err := readFleet()
	if err != nil {
		return nil, err
	}
	idx := inv.index(id)
	block, managed := ff.find(id)
	if idx < 0 && block == nil {
		return nil, &apiError{Status: http.StatusNotFound, Code: "not_found", Msg: "unknown node " + id}
	}
	if idx >= 0 {
		obj, err := inv.object(idx)
		if err != nil {
			return nil, err
		}
		if obj.str("guardian_role") == "hub" {
			return nil, &apiError{Status: http.StatusForbidden, Code: "hub_protected", Msg: "the hub cannot be removed from the dashboard"}
		}
	}
	if block != nil && !managed {
		return nil, unmanagedConflict(id)
	}

	known, _, err := readFileIfExists(knownHostsPath())
	if err != nil {
		return nil, err
	}
	removed := map[string]bool{"fleet_block": false, "known_hosts": false, "inventory": false}
	var t txn
	if err := t.track(knownHostsPath(), fleetFilePath(), invPath); err != nil {
		return nil, err
	}
	var werr error
	if managed {
		ff.remove(id)
		werr = writeFileAtomic(fleetFilePath(), []byte(ff.render()), true)
		removed["fleet_block"] = werr == nil
	}
	if werr == nil {
		if after := knownHostsWithout(string(known), id); after != string(known) {
			werr = writeFileAtomic(knownHostsPath(), []byte(after), true)
			removed["known_hosts"] = werr == nil
		}
	}
	if werr == nil && idx >= 0 {
		inv.remove(idx)
		werr = inv.save()
		removed["inventory"] = werr == nil
	}
	if werr != nil {
		return nil, t.failRolledBack("write_failed", "removing the node failed: "+werr.Error())
	}
	return removed, nil
}

// ---- POST /api/nodes/{id}/probe ----

// handleNodeProbe is the "Test again" button: it re-runs the probe for an inventory node.
func handleNodeProbe(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := validateNodeID(id); err != nil {
		writeAPIError(w, err)
		return
	}
	nodes, err := loadNodes()
	if err != nil {
		writeAPIError(w, err)
		return
	}
	known := false
	for _, s := range nodes {
		known = known || s.ID == id
	}
	if !known {
		writeAPIError(w, &apiError{Status: http.StatusNotFound, Code: "not_found", Msg: "unknown node " + id})
		return
	}
	resp := addProbe(map[string]any{"ok": true, "id": id}, id)
	jsonResponse(w, http.StatusOK, resp)
}

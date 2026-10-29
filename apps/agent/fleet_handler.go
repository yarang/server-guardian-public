package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// errUnknownHost is returned when a request names a host that is not in servers.json.
var errUnknownHost = errors.New("unknown target host")

// safeSSHHost reports whether h can be handed to ssh as a destination: non-empty, not
// option-like, and free of whitespace and control characters.
func safeSSHHost(h string) bool {
	if h == "" || strings.HasPrefix(h, "-") {
		return false
	}
	for _, c := range h {
		if c <= ' ' || c == 0x7f {
			return false
		}
	}
	return true
}

// resolveTargetHost accepts a request-supplied host only when it exactly matches the inventory
// id of a node in servers.json, so a request can never steer ssh to an arbitrary destination
// or smuggle ssh options (e.g. "-oProxyCommand=..."). A name or hostname is not a target: ssh
// uses the id as the Host alias, and the fleet routes already match id/name only. When the text
// matches a node's name or hostname the error carries a hint naming that node's id.
func resolveTargetHost(nodes []FleetNode, target string) (string, error) {
	if !safeSSHHost(target) {
		return "", fmt.Errorf("%w: %q", errUnknownHost, target)
	}
	for _, s := range nodes {
		if target == s.ID {
			return s.ID, nil
		}
	}
	for _, s := range nodes {
		if target == s.Name || target == s.Hostname {
			return "", fmt.Errorf("%w: %q is not an id; use the node id %q", errUnknownHost, target, s.ID)
		}
	}
	return "", fmt.Errorf("%w: %q is not in servers.json", errUnknownHost, target)
}

// unknownTargetsError is the errUnknownHost error for targets that matched nothing.
func unknownTargetsError(unknown []string) error {
	return fmt.Errorf("%w: %s", errUnknownHost, strings.Join(unknown, ", "))
}

// sshArgs builds the ssh argv for a host that is trusted on first use. "--" ends option parsing,
// so the host can never be read as an ssh option even if it starts with "-".
func sshArgs(targetHost, remoteCmd string) []string {
	return sshArgsFor(targetHost, remoteCmd, false)
}

// sshArgsFor is sshArgs with the host key policy chosen per target. A pinned node (registered from
// the dashboard) gets no StrictHostKeyChecking option here, because a command-line -o would override
// the fleet block's "StrictHostKeyChecking yes", HostKeyAlias and UserKnownHostsFile.
func sshArgsFor(targetHost, remoteCmd string, pinned bool) []string {
	args := []string{"-o", "BatchMode=yes", "-o", "ConnectTimeout=4"}
	if !pinned {
		args = append(args, "-o", "StrictHostKeyChecking=accept-new")
	}
	return append(args, "--", targetHost, remoteCmd)
}

// hostKeyOverride decides whether ssh gets "-o StrictHostKeyChecking=accept-new" for host. A
// command-line -o beats the fleet block, so the override must be limited to hosts for which
// trust-on-first-use is the intended policy:
//
//   - no block for the alias (a host only the Mac-style ssh config or DNS knows): accept-new, as
//     the original design intends for genuinely unknown hosts;
//   - a managed "migrated" block (written by scripts/setup-hub-ssh.sh, no pinned key): accept-new,
//     which is what that block asks for anyway;
//   - a managed "registered" block (pinned key, StrictHostKeyChecking yes) or a hand-written block:
//     no override, so the block's own policy (strict checking, HostKeyAlias, known-hosts file) holds.
//
// A missing fleet file means no blocks. A fleet file that exists but cannot be read fails closed:
// returning an error (no ssh is started) is safer than silently downgrading pinned nodes.
func hostKeyOverride(host string) (bool, error) {
	data, exists, err := readFileIfExists(fleetFilePath())
	if err != nil {
		return false, fmt.Errorf("fleet ssh file unreadable, refusing to connect without its host key policy: %w", err)
	}
	if !exists {
		return true, nil
	}
	blk, managed := parseFleetFile(string(data)).find(host)
	if blk == nil {
		return true, nil
	}
	return managed && blk.Shape == shapeMigrated, nil
}

// isPinnedHost reports whether host must not get the accept-new override (see hostKeyOverride).
// Kept for callers that only need the boolean; an unreadable fleet file reports false here and is
// refused separately by executeSSH.
func isPinnedHost(host string) bool {
	override, err := hostKeyOverride(host)
	return err == nil && !override
}

// @MX:WARN: [AUTO] Runs remoteCmd on targetHost over the fleet SSH key (root-capable on every node).
// @MX:REASON: Callers must pass a host from servers.json (resolveTargetHost or a FleetNode.ID); option-like hosts are refused here as a last line of defence.
func executeSSH(targetHost string, remoteCmd string, timeoutSec int) (int, string, string, int64) {
	if timeoutSec <= 0 {
		timeoutSec = 10
	}
	if !safeSSHHost(targetHost) {
		return 255, "", fmt.Sprintf("refusing unsafe ssh target %q", targetHost), 0
	}
	override, herr := hostKeyOverride(targetHost)
	if herr != nil {
		return 255, "", herr.Error(), 0
	}
	start := time.Now()
	cmd := exec.Command("ssh", sshArgsFor(targetHost, remoteCmd, !override)...)
	stdout, stderr := newCappedBuffer(maxCommandOutput), newCappedBuffer(maxCommandOutput)
	cmd.Stdout = stdout
	cmd.Stderr = stderr

	timer := time.AfterFunc(time.Duration(timeoutSec)*time.Second, func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	})
	defer timer.Stop()

	err := cmd.Run()
	duration := time.Since(start).Milliseconds()

	exitCode := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		} else {
			exitCode = 1
		}
	}
	return exitCode, stdout.String(), stderr.String(), duration
}

type FleetNode struct {
	ID              string   `json:"id"`
	Name            string   `json:"name"`
	Hostname        string   `json:"hostname"`
	User            string   `json:"user"`
	Port            int      `json:"port"`
	Arch            string   `json:"arch"`
	Group           string   `json:"group"`
	Key             string   `json:"key"`
	Recommended     bool     `json:"recommended"`
	Description     string   `json:"description"`
	TailscaleIP     string   `json:"tailscale_ip,omitempty"`
	TailscaleTags   []string `json:"tailscale_tags,omitempty"`
	TailscaleDomain string   `json:"tailscale_domain,omitempty"`
	// Inventory fields that node group rules match on (docs/design/node-groups.md).
	Tier         string   `json:"tier,omitempty"`
	Role         string   `json:"role,omitempty"`
	GuardianRole string   `json:"guardian_role,omitempty"`
	CLISupport   []string `json:"cli_support,omitempty"`
	// OpsRoles name the roles of the host in configs/package-roles.json; they protect packages from removal.
	OpsRoles []string `json:"ops_roles,omitempty"`
	// Set by /api/nodes (docs/design/node-registration.md): where hostname/user/port come from
	// ("fleet_file" or "inventory") and, for fleet file nodes, whether the host key is "pinned"
	// (registered from the dashboard) or trusted on first use ("accept-new", migrated).
	Connection  string `json:"connection,omitempty"`
	HostKeyMode string `json:"host_key_mode,omitempty"`
	// HubKeyRevokedAt is the RFC3339 time a dashboard revoke-key succeeded for this node, or null
	// while the hub key is still believed installed on it (docs/design/node-registration.md).
	HubKeyRevokedAt *string `json:"hub_key_revoked_at"`
}

type RemoteExecRequest struct {
	TargetHost string `json:"target_host"`
	Command    string `json:"command"`
	TimeoutSec int    `json:"timeout_sec,omitempty"`
}

// serversFile returns the absolute path of the servers.json the agent reads. When
// GUARDIAN_SERVERS_FILE is set it is the only candidate: a missing or non-regular file is an
// error naming the path, never a silent fall back to a stale checkout copy (the hub's command
// allow-list and node groups must come from the file the dashboard writes). Only when the
// variable is unset does it look for configs/servers.json relative to the working directory (or
// its parents), then ~/.config/agentthread/servers.json (development setups). The debate CLI gets
// the same path (debateHub.newCmd), so node group membership is computed against the same inventory.
func serversFile() (string, error) {
	if env := os.Getenv("GUARDIAN_SERVERS_FILE"); env != "" {
		st, err := os.Stat(env)
		if err != nil {
			return "", fmt.Errorf("GUARDIAN_SERVERS_FILE %s is not usable: %w", env, err)
		}
		if st.IsDir() {
			return "", fmt.Errorf("GUARDIAN_SERVERS_FILE %s is a directory", env)
		}
		if abs, err := filepath.Abs(env); err == nil {
			return abs, nil
		}
		return env, nil
	}
	candidates := []string{"configs/servers.json", "../../configs/servers.json", "../configs/servers.json"}
	if home, err := os.UserHomeDir(); err == nil {
		candidates = append(candidates, filepath.Join(home, ".config/agentthread/servers.json"))
	}
	for _, c := range candidates {
		if st, err := os.Stat(c); err == nil && !st.IsDir() {
			if abs, err := filepath.Abs(c); err == nil {
				return abs, nil
			}
			return c, nil
		}
	}
	return "", errors.New("servers.json not found")
}

func loadNodes() ([]FleetNode, error) {
	path, err := serversFile()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("servers.json not readable: %w", err)
	}
	var nodes []FleetNode
	if err := json.Unmarshal(data, &nodes); err != nil {
		return nil, err
	}
	return nodes, nil
}

// cleanTargets trims target ids and drops empty entries.
func cleanTargets(raw []string) []string {
	var out []string
	for _, t := range raw {
		if t = strings.TrimSpace(t); t != "" {
			out = append(out, t)
		}
	}
	return out
}

// targetsFromQuery reads ?targets=a,b (repeatable) for GET endpoints. Node ids are not
// sensitive, unlike commands, so a query string is fine here.
func targetsFromQuery(r *http.Request) []string {
	var raw []string
	for _, v := range r.URL.Query()["targets"] {
		raw = append(raw, strings.Split(v, ",")...)
	}
	return cleanTargets(raw)
}

// fleetSubset loads servers.json and narrows it to targets (all nodes when empty). Unknown
// targets are an errUnknownHost error, never a silently smaller run.
func fleetSubset(targets []string) ([]FleetNode, error) {
	all, err := loadNodes()
	if err != nil {
		return nil, err
	}
	nodes, unknown := selectNodes(all, targets)
	if len(unknown) > 0 {
		return nil, fmt.Errorf("%w: %s", errUnknownHost, strings.Join(unknown, ", "))
	}
	return nodes, nil
}

// fleetErrorStatus maps a fleet run error to an HTTP status: unknown targets are the
// caller's mistake (400), anything else (servers.json unreadable) is ours (500).
func fleetErrorStatus(err error) int {
	if errors.Is(err, errUnknownHost) {
		return http.StatusBadRequest
	}
	return http.StatusInternalServerError
}

type FleetNodeResult struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Group  string `json:"group"`
	Arch   string `json:"arch"`
	Status string `json:"status"` // "ONLINE", "OFFLINE"
	// Reachability is classifySSH of the probe: ok, auth_failed, host_key_changed, unreachable or
	// command_failed. Status stays ONLINE only for ok; a node whose ssh worked but whose probe
	// command failed (e.g. no `free` on macOS) reads OFFLINE with reachability command_failed.
	Reachability string `json:"reachability"`
	OsArch       string `json:"os_arch"`
	Uptime       string `json:"uptime"`
	LoadAvg      string `json:"load_avg"`
	Memory       string `json:"memory"`
	Disk         string `json:"disk"`
	DurationMs   int64  `json:"duration_ms"`
}

type FleetDiagnosisReport struct {
	TotalNodes  int               `json:"total_nodes"`
	OnlineCount int               `json:"online_count"`
	DurationSec float64           `json:"duration_sec"`
	Results     []FleetNodeResult `json:"results"`
}

type FleetExecRequest struct {
	Command    string `json:"command"`
	TimeoutSec int    `json:"timeout_sec,omitempty"`
	// Targets narrows the run to these node ids/names (a resolved node group); empty means all.
	Targets []string `json:"targets,omitempty"`
}

type FleetExecResult struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	Group      string `json:"group"`
	Arch       string `json:"arch"`
	ExitCode   int    `json:"exit_code"`
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	DurationMs int64  `json:"duration_ms"`
	// Reachability is classifySSH(exit_code, stderr): ok, auth_failed, host_key_changed,
	// unreachable or command_failed.
	Reachability string `json:"reachability"`
}

type FleetExecReport struct {
	Command      string            `json:"command"`
	TotalNodes   int               `json:"total_nodes"`
	SuccessCount int               `json:"success_count"`
	FailedCount  int               `json:"failed_count"`
	DurationSec  float64           `json:"duration_sec"`
	Results      []FleetExecResult `json:"results"`
}

// DiagnoseFleet probes every node, or only targets (exact ids/names from servers.json).
func DiagnoseFleet(targets []string) (FleetDiagnosisReport, error) {
	nodes, err := fleetSubset(targets)
	if err != nil {
		return FleetDiagnosisReport{}, err
	}

	start := time.Now()
	var wg sync.WaitGroup
	results := make([]FleetNodeResult, len(nodes))

	probeScript := `uname -sm && uptime && free -m | awk 'NR==2{printf "%s/%sMB (%.0f%%)", $3,$2,$3*100/$2}' && echo "" && df -h / | awk 'NR==2{print $5}'`

	for i, s := range nodes {
		wg.Add(1)
		go func(i int, srv FleetNode) {
			defer wg.Done()
			exitCode, stdout, probeErr, dur := executeSSH(srv.ID, probeScript, 7)

			status := "OFFLINE"
			osArch := ""
			uptimeStr := ""
			loadAvg := ""
			memStr := ""
			diskStr := ""

			if exitCode == 0 {
				status = "ONLINE"
				lines := strings.Split(strings.TrimSpace(stdout), "\n")
				if len(lines) > 0 {
					osArch = strings.TrimSpace(lines[0])
				}
				if len(lines) > 1 {
					uptimeStr = strings.TrimSpace(lines[1])
					if idx := strings.Index(uptimeStr, "load average:"); idx != -1 {
						loadAvg = strings.TrimSpace(uptimeStr[idx+len("load average:"):])
					}
				}
				if len(lines) > 2 {
					memStr = strings.TrimSpace(lines[2])
				}
				if len(lines) > 3 {
					diskStr = strings.TrimSpace(lines[3])
				}
			}

			results[i] = FleetNodeResult{
				ID:           srv.ID,
				Name:         srv.Name,
				Group:        srv.Group,
				Arch:         srv.Arch,
				Status:       status,
				Reachability: classifySSH(exitCode, probeErr),
				OsArch:       osArch,
				Uptime:       uptimeStr,
				LoadAvg:      loadAvg,
				Memory:       memStr,
				Disk:         diskStr,
				DurationMs:   dur,
			}
		}(i, s)
	}

	wg.Wait()
	durationSec := time.Since(start).Seconds()

	onlineCount := 0
	for _, r := range results {
		if r.Status == "ONLINE" {
			onlineCount++
		}
	}

	return FleetDiagnosisReport{
		TotalNodes:  len(results),
		OnlineCount: onlineCount,
		DurationSec: durationSec,
		Results:     results,
	}, nil
}

// ExecFleet runs cmd on every node, or only targets (exact ids/names from servers.json).
func ExecFleet(cmd string, timeoutSec int, targets []string) (FleetExecReport, error) {
	all, err := loadNodes()
	if err != nil {
		return FleetExecReport{}, err
	}
	nodes, err := execNodes(all, targets)
	if err != nil {
		return FleetExecReport{}, err
	}
	if timeoutSec <= 0 {
		timeoutSec = 10
	}

	start := time.Now()
	var wg sync.WaitGroup
	results := make([]FleetExecResult, len(nodes))

	for i, s := range nodes {
		wg.Add(1)
		go func(i int, srv FleetNode) {
			defer wg.Done()
			exitCode, stdout, stderr, dur := executeSSH(srv.ID, cmd, timeoutSec)

			results[i] = FleetExecResult{
				ID:           srv.ID,
				Name:         srv.Name,
				Group:        srv.Group,
				Arch:         srv.Arch,
				ExitCode:     exitCode,
				Stdout:       strings.TrimSpace(stdout),
				Stderr:       strings.TrimSpace(stderr),
				DurationMs:   dur,
				Reachability: classifySSH(exitCode, stderr),
			}
		}(i, s)
	}

	wg.Wait()
	durationSec := time.Since(start).Seconds()

	successCount := 0
	for _, r := range results {
		if r.ExitCode == 0 {
			successCount++
		}
	}

	return FleetExecReport{
		Command:      cmd,
		TotalNodes:   len(results),
		SuccessCount: successCount,
		FailedCount:  len(results) - successCount,
		DurationSec:  durationSec,
		Results:      results,
	}, nil
}

// commandBodyLimit caps the JSON body of the command endpoints (/api/exec, /api/exec/remote,
// /api/fleet/exec and its stream); a command plus targets is far smaller.
const commandBodyLimit = 64 << 10

// @MX:ANCHOR: [AUTO] Single body decoder for every command endpoint (fan_in 4).
// @MX:REASON: The size cap and 413/400 split must stay identical across /api/exec, /api/exec/remote and the fleet routes.
// decodeCommandBody decodes a command request body into v. On failure it writes 413 for an
// oversized body or 400 otherwise, and returns false.
func decodeCommandBody(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, commandBodyLimit)).Decode(v); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			jsonResponse(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "request body too large"})
		} else {
			jsonResponse(w, http.StatusBadRequest, map[string]string{"error": "Invalid JSON body"})
		}
		return false
	}
	return true
}

// FleetStreamRequest is the JSON body of POST /api/fleet/exec/stream.
type FleetStreamRequest struct {
	Command    string   `json:"command"`
	TimeoutSec int      `json:"timeout_sec,omitempty"`
	Targets    []string `json:"targets,omitempty"`
}

// @MX:NOTE: [AUTO] POST only: the command and targets travel in a JSON body, never in the URL, so they
// stay out of access logs and browser history, and the request gets the CORS preflight and
// Content-Type checks of withSecurity. Clients read the SSE body with fetch, not EventSource.
func handleFleetExecStream(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
		return
	}
	var req FleetStreamRequest
	if !decodeCommandBody(w, r, &req) {
		recordRejected(r, "/api/fleet/exec/stream", nil, "", "invalid or oversized JSON body", time.Now())
		return
	}
	cmd := req.Command
	if strings.TrimSpace(cmd) == "" {
		recordRejected(r, "/api/fleet/exec/stream", cleanTargets(req.Targets), "", "command required", time.Now())
		jsonResponse(w, http.StatusBadRequest, map[string]string{"error": "command required"})
		return
	}
	// Targets are checked against servers.json before the stream starts so an unknown
	// target is a plain 400 instead of a silently narrowed fleet run.
	targets := cleanTargets(req.Targets)
	if len(targets) > 0 {
		all, err := loadNodes()
		if err == nil {
			_, err = execNodes(all, targets)
		}
		if err != nil {
			if isRejection(err) {
				recordRejected(r, "/api/fleet/exec/stream", targets, cmd, err.Error(), time.Now())
			}
			jsonResponse(w, execErrorStatus(err), map[string]string{"error": err.Error()})
			return
		}
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	var writeMu sync.Mutex
	sendSSE := func(evt FleetStreamEvent) {
		writeMu.Lock()
		defer writeMu.Unlock()
		data, _ := json.Marshal(evt)
		fmt.Fprintf(w, "event: %s\ndata: %s\n\n", evt.Type, data)
		flusher.Flush()
	}
	auditStart := time.Now()
	codes := map[string]int{}
	var codesMu sync.Mutex
	_ = StreamExecFleet(cmd, req.TimeoutSec, targets, func(evt FleetStreamEvent) {
		if evt.Type == "node_done" && evt.Result != nil {
			codesMu.Lock()
			codes[evt.Result.ID] = evt.Result.ExitCode
			codesMu.Unlock()
		}
		sendSSE(evt)
	})
	codesMu.Lock()
	recordExec(r, "/api/fleet/exec/stream", sortedKeys(codes), cmd, codes, auditStart)
	codesMu.Unlock()
}

type FleetStreamEvent struct {
	Type       string           `json:"type"` // "start", "node_start", "node_done", "complete", "error"
	Command    string           `json:"command,omitempty"`
	Total      int              `json:"total,omitempty"`
	Completed  int              `json:"completed,omitempty"`
	Success    int              `json:"success,omitempty"`
	Failed     int              `json:"failed,omitempty"`
	ElapsedSec float64          `json:"elapsed_sec,omitempty"`
	Node       *FleetNode       `json:"node,omitempty"`
	Result     *FleetExecResult `json:"result,omitempty"`
	Message    string           `json:"message,omitempty"`
}

// selectNodes returns the nodes matching any target by exact id or name (all nodes
// when targets is empty) and the targets that matched nothing. The legacy servers.json
// "group" string is deliberately not matched: node groups are user-defined and resolved to
// explicit ids by the caller (UI, CLI), so there is a single notion of group.
func selectNodes(all []FleetNode, targets []string) ([]FleetNode, []string) {
	if len(targets) == 0 {
		return all, nil
	}
	var unknown []string
	want := make(map[string]bool)
	for _, t := range targets {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		matched := false
		for _, s := range all {
			if t == s.ID || t == s.Name {
				matched = true
				break
			}
		}
		if !matched {
			unknown = append(unknown, t)
		}
		want[t] = true
	}
	var out []FleetNode
	for _, s := range all {
		if want[s.ID] || want[s.Name] {
			out = append(out, s)
		}
	}
	return out, unknown
}

// streamSSHExec runs one SSH command for StreamExecFleet; tests replace it.
var streamSSHExec = executeSSH

func StreamExecFleet(cmd string, timeoutSec int, targetIDs []string, onEvent func(FleetStreamEvent)) error {
	allNodes, err := loadNodes()
	if err != nil {
		onEvent(FleetStreamEvent{Type: "error", Message: err.Error()})
		return err
	}
	if timeoutSec <= 0 {
		timeoutSec = 10
	}

	nodes, err := execNodes(allNodes, targetIDs)
	if err != nil {
		onEvent(FleetStreamEvent{Type: "error", Message: err.Error()})
		return err
	}

	if len(nodes) == 0 {
		onEvent(FleetStreamEvent{Type: "error", Message: "No matching servers found"})
		return fmt.Errorf("no matching servers found")
	}

	start := time.Now()
	total := len(nodes)
	onEvent(FleetStreamEvent{
		Type:    "start",
		Command: cmd,
		Total:   total,
	})

	var wg sync.WaitGroup
	var mu sync.Mutex
	completed := 0
	success := 0
	failed := 0

	for _, s := range nodes {
		wg.Add(1)
		go func(srv FleetNode) {
			defer wg.Done()
			onEvent(FleetStreamEvent{
				Type: "node_start",
				Node: &srv,
			})

			exitCode, stdout, stderr, dur := streamSSHExec(srv.ID, cmd, timeoutSec)

			res := FleetExecResult{
				ID:           srv.ID,
				Name:         srv.Name,
				Group:        srv.Group,
				Arch:         srv.Arch,
				ExitCode:     exitCode,
				Stdout:       strings.TrimSpace(stdout),
				Stderr:       strings.TrimSpace(stderr),
				DurationMs:   dur,
				Reachability: classifySSH(exitCode, stderr),
			}

			mu.Lock()
			completed++
			if exitCode == 0 {
				success++
			} else {
				failed++
			}
			curCompleted := completed
			curSuccess := success
			curFailed := failed
			mu.Unlock()

			onEvent(FleetStreamEvent{
				Type:       "node_done",
				Completed:  curCompleted,
				Total:      total,
				Success:    curSuccess,
				Failed:     curFailed,
				ElapsedSec: time.Since(start).Seconds(),
				Node:       &srv,
				Result:     &res,
			})
		}(s)
	}

	wg.Wait()
	durationSec := time.Since(start).Seconds()

	onEvent(FleetStreamEvent{
		Type:       "complete",
		Command:    cmd,
		Total:      total,
		Completed:  total,
		Success:    success,
		Failed:     failed,
		ElapsedSec: durationSec,
	})

	return nil
}

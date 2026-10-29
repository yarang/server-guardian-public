package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

// AgentVersion is stamped at build time by scripts/install-agent-service.sh and scripts/ci.sh:
//
//	go build -ldflags "-X main.AgentVersion=$(git describe --always --dirty)"
//
// A plain `go build` keeps this default.
var AgentVersion = "v0.1.0-alpha"

func jsonResponse(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}

func main() {
	port := os.Getenv("AGENT_PORT")
	if port == "" {
		port = "9870"
	}
	// @MX:WARN: [AUTO] /api/exec runs arbitrary shell commands as the agent user.
	// @MX:REASON: Listen on loopback by default; set AGENT_HOST (e.g. "127.0.0.1,<Tailscale IP>") to expose it on a private mesh only.
	hosts := listenHosts(os.Getenv("AGENT_HOST"))

	// Fail closed before binding anything: no token means no start, except an explicit
	// loopback-only dev opt-in.
	token := os.Getenv("AGENT_TOKEN")
	if strings.TrimSpace(token) == "" {
		token = ""
	}
	if err := checkAuthMode(token, os.Getenv("GUARDIAN_INSECURE_NO_AUTH"), hosts); err != nil {
		log.Fatal(err)
	}
	if token == "" {
		log.Printf("⚠️  GUARDIAN_INSECURE_NO_AUTH=1: /api is unauthenticated on loopback %v", hosts)
	}

	mux := http.NewServeMux()

	// 1. Status & Heartbeat
	mux.HandleFunc("GET /api/status", func(w http.ResponseWriter, r *http.Request) {
		metrics, _ := CollectMetrics()
		health, reasons := nodeHealth(metrics)
		status := NodeStatus{
			AgentVersion: AgentVersion,
			Status:       health,
			Reasons:      reasons,
			AgentUptime:  int64(time.Since(agentStartTime).Seconds()),
			Metrics:      metrics,
		}
		jsonResponse(w, http.StatusOK, status)
	})

	// 2. Real-time Metrics
	mux.HandleFunc("GET /api/metrics", func(w http.ResponseWriter, r *http.Request) {
		metrics, err := CollectMetrics()
		if err != nil {
			jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, http.StatusOK, metrics)
	})

	// 3. SSH Config Summary
	mux.HandleFunc("GET /api/ssh/config", func(w http.ResponseWriter, r *http.Request) {
		cfg, err := ParseSSHDConfig("")
		if err != nil {
			jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, http.StatusOK, cfg)
	})

	// 4. SSH Security Audit
	mux.HandleFunc("GET /api/ssh/security", func(w http.ResponseWriter, r *http.Request) {
		cfg, _ := ParseSSHDConfig("")
		report := EvaluateSecurity(cfg)
		jsonResponse(w, http.StatusOK, report)
	})

	// 5. Authorized Keys
	mux.HandleFunc("GET /api/ssh/keys", func(w http.ResponseWriter, r *http.Request) {
		keys, err := GetAuthorizedKeys()
		if err != nil {
			jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, http.StatusOK, map[string]interface{}{
			"count": len(keys),
			"keys":  keys,
		})
	})

	// 6. Execute Remote Command
	mux.HandleFunc("/api/exec", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
			return
		}
		var req CommandRequest
		if !decodeCommandBody(w, r, &req) {
			recordRejected(r, "/api/exec", []string{"local"}, "", "invalid or oversized JSON body", time.Now())
			return
		}
		if req.Command == "" {
			recordRejected(r, "/api/exec", []string{"local"}, "", "command cannot be empty", time.Now())
			jsonResponse(w, http.StatusBadRequest, map[string]string{"error": "Command cannot be empty"})
			return
		}

		auditStart := time.Now()
		exitCode, stdout, stderr, dur := executeRaw(req.Command, req.Timeout)
		recordExec(r, "/api/exec", []string{"local"}, req.Command, map[string]int{"local": exitCode}, auditStart)
		res := CommandResponse{
			ExitCode:   exitCode,
			Stdout:     stdout,
			Stderr:     stderr,
			DurationMs: dur,
		}
		jsonResponse(w, http.StatusOK, res)
	})

	// 8. Fleet node inventory
	mux.HandleFunc("GET /api/nodes", func(w http.ResponseWriter, r *http.Request) {
		nodes, err := loadNodesWithConnection()
		if err != nil {
			jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, http.StatusOK, map[string]interface{}{
			"count": len(nodes),
			"nodes": nodes,
		})
	})

	// 8-1. Fleet-wide Parallel Diagnosis
	mux.HandleFunc("GET /api/fleet/diagnose", func(w http.ResponseWriter, r *http.Request) {
		report, err := DiagnoseFleet(targetsFromQuery(r))
		if err != nil {
			jsonResponse(w, fleetErrorStatus(err), map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, http.StatusOK, report)
	})

	// 8-2. Fleet-wide Parallel Command Execution
	mux.HandleFunc("/api/fleet/exec", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
			return
		}
		var req FleetExecRequest
		if !decodeCommandBody(w, r, &req) {
			recordRejected(r, "/api/fleet/exec", nil, "", "invalid or oversized JSON body", time.Now())
			return
		}
		if strings.TrimSpace(req.Command) == "" {
			recordRejected(r, "/api/fleet/exec", cleanTargets(req.Targets), "", "command required", time.Now())
			jsonResponse(w, http.StatusBadRequest, map[string]string{"error": "command required"})
			return
		}
		auditStart := time.Now()
		report, err := ExecFleet(req.Command, req.TimeoutSec, cleanTargets(req.Targets))
		auditCodes := map[string]int{}
		for _, res := range report.Results {
			auditCodes[res.ID] = res.ExitCode
		}
		if err != nil && isRejection(err) {
			recordRejected(r, "/api/fleet/exec", cleanTargets(req.Targets), req.Command, err.Error(), auditStart)
		} else {
			recordExec(r, "/api/fleet/exec", sortedKeys(auditCodes), req.Command, auditCodes, auditStart)
		}
		if err != nil {
			jsonResponse(w, execErrorStatus(err), map[string]string{"error": err.Error()})
			return
		}
		jsonResponse(w, http.StatusOK, report)
	})

	// 8-3. Fleet-wide Command Execution SSE Stream (Real-Time Progress & Logs)
	mux.HandleFunc("/api/fleet/exec/stream", handleFleetExecStream)

	// 8-4. Fleet-wide SSH security audit (fixed read-only probe, 60s cache, ?refresh=1)
	mux.HandleFunc("/api/fleet/ssh", handleFleetSSH)

	// 9. Execute Remote SSH Command on Target Fleet Host
	mux.HandleFunc("/api/exec/remote", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			jsonResponse(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
			return
		}
		var req RemoteExecRequest
		if !decodeCommandBody(w, r, &req) {
			recordRejected(r, "/api/exec/remote", nil, "", "invalid or oversized JSON body", time.Now())
			return
		}
		if req.TargetHost == "" || req.Command == "" {
			recordRejected(r, "/api/exec/remote", []string{req.TargetHost}, req.Command, "target_host and command required", time.Now())
			jsonResponse(w, http.StatusBadRequest, map[string]string{"error": "target_host and command required"})
			return
		}
		nodes, err := loadNodes()
		if err != nil {
			jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		target, err := resolveTargetHost(nodes, req.TargetHost)
		if err != nil {
			recordRejected(r, "/api/exec/remote", []string{req.TargetHost}, req.Command, err.Error(), time.Now())
			jsonResponse(w, http.StatusBadRequest, map[string]string{"error": err.Error() + " (target_host must be a server id from servers.json)"})
			return
		}
		if hubSudoOff() && hubIDIn(nodes, target) {
			recordRejected(r, "/api/exec/remote", []string{target}, req.Command, errHubNotTarget.Error(), time.Now())
			jsonResponse(w, http.StatusConflict, map[string]string{"error": errHubNotTarget.Error()})
			return
		}
		timeout := req.TimeoutSec
		if timeout <= 0 {
			timeout = 10
		}
		auditStart := time.Now()
		exitCode, stdout, stderr, dur := executeSSH(target, req.Command, timeout)
		recordExec(r, "/api/exec/remote", []string{target}, req.Command, map[string]int{target: exitCode}, auditStart)
		jsonResponse(w, http.StatusOK, CommandResponse{
			ExitCode:   exitCode,
			Stdout:     stdout,
			Stderr:     stderr,
			DurationMs: dur,
		})
	})

	// 10. Server-Sent Events (SSE) for Real-Time Telemetry Stream
	mux.HandleFunc("GET /api/stream/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("Connection", "keep-alive")

		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "Streaming unsupported", http.StatusInternalServerError)
			return
		}

		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-r.Context().Done():
				return
			case <-ticker.C:
				m, _ := CollectMetrics()
				payload, _ := json.Marshal(m)
				fmt.Fprintf(w, "data: %s\n\n", payload)
				flusher.Flush()
			}
		}
	})

	// 11. Debate teams: personas, teams and debate jobs (driven by the Python CLI)
	registerDebateRoutes(mux)

	mux.HandleFunc("GET /api/audit", handleAudit)

	// 12. Node registration: fleet SSH file, pinned host keys and the hub inventory
	registerNodeRoutes(mux)
	registerMeshRoutes(mux)

	if ok, err := registerUI(mux, os.Getenv("AGENT_UI_DIR")); err != nil {
		log.Fatalf("AGENT_UI_DIR is set but has no index.html: %v", err)
	} else if ok {
		log.Printf("🖥️  Serving web dashboard from %s", os.Getenv("AGENT_UI_DIR"))
	}

	// withSecurity (Host, Origin, CORS, Content-Type) runs before withAuth so preflights and
	// cross-site requests are settled without touching credentials.
	handler := withSecurity(withAuth(mux, token), securityConfigFromEnv())

	// Any listener failing (e.g. the Tailscale IP not assigned yet) exits the process so
	// systemd restarts it with every address bound.
	errc := make(chan error, len(hosts))
	for _, host := range hosts {
		serverAddr := net.JoinHostPort(host, port)
		log.Printf("🛡️  Server Guardian Agent %s listening on %s (PID: %d)", AgentVersion, serverAddr, os.Getpid())
		go func(addr string) { errc <- http.ListenAndServe(addr, handler) }(serverAddr)
	}
	log.Fatalf("Server Guardian Agent error: %v", <-errc)
}

// listenHosts parses a comma-separated AGENT_HOST value, defaulting to loopback.
func listenHosts(raw string) []string {
	var hosts []string
	for _, h := range strings.Split(raw, ",") {
		if h = strings.TrimSpace(h); h != "" {
			hosts = append(hosts, h)
		}
	}
	if len(hosts) == 0 {
		return []string{"127.0.0.1"}
	}
	return hosts
}

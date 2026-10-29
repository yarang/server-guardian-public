package main

import "time"

// SystemMetrics represents system telemetries
type SystemMetrics struct {
	Timestamp   time.Time     `json:"timestamp"`
	Hostname    string        `json:"hostname"`
	OS          string        `json:"os"`
	Arch        string        `json:"arch"`
	UptimeSec   uint64        `json:"uptime_sec"`
	CPUUsagePct float64       `json:"cpu_usage_pct"`
	MemoryTotal uint64        `json:"memory_total_bytes"`
	MemoryUsed  uint64        `json:"memory_used_bytes"`
	MemoryPct   float64       `json:"memory_pct"`
	DiskTotal   uint64        `json:"disk_total_bytes"`
	DiskUsed    uint64        `json:"disk_used_bytes"`
	DiskPct     float64       `json:"disk_pct"`
	LoadAvg     [3]float64    `json:"load_avg"`
	TopProcesses []ProcessInfo `json:"top_processes"`
	// Issues names the values this host could not read; their fields are zero, not estimates.
	Issues []string `json:"issues,omitempty"`
}

// ProcessInfo represents a running process summary
type ProcessInfo struct {
	PID     int     `json:"pid"`
	User    string  `json:"user"`
	CPU     float64 `json:"cpu"`
	Memory  float64 `json:"mem"`
	Command string  `json:"command"`
}

// SSHConfigSummary contains parsed sshd configuration highlights
type SSHConfigSummary struct {
	ConfigPath        string   `json:"config_path"`
	Port              int      `json:"port"`
	PermitRootLogin   string   `json:"permit_root_login"`
	PasswordAuth      string   `json:"password_authentication"`
	PubkeyAuth        string   `json:"pubkey_authentication"`
	MaxAuthTries      int      `json:"max_auth_tries"`
	X11Forwarding     string   `json:"x11_forwarding"`
	AllowTcpForwarding string   `json:"allow_tcp_forwarding"`
	// IsValid is only meaningful when SyntaxChecked is true.
	IsValid       bool   `json:"is_valid"`
	SyntaxChecked bool   `json:"syntax_checked"`
	SyntaxMessage string `json:"syntax_message"`
	// Source is where the values came from: "sshd -T" (effective config) or "config files".
	Source string `json:"source"`
	RawLinesCount     int      `json:"raw_lines_count"`
}

// AuthorizedKey represents an entry in authorized_keys
type AuthorizedKey struct {
	ID        string    `json:"id"`
	KeyType   string    `json:"key_type"`
	PublicKey string    `json:"public_key"`
	Comment   string    `json:"comment"`
	Fingerprint string  `json:"fingerprint"`
	// Options are the restrictions before the key type, e.g. from="100.64.0.7",no-agent-forwarding.
	Options string `json:"options,omitempty"`
}

// CommandRequest represents an execution request
type CommandRequest struct {
	Command string `json:"command"`
	Timeout int    `json:"timeout_sec,omitempty"` // default 10s
}

// CommandResponse represents execution output
type CommandResponse struct {
	ExitCode   int    `json:"exit_code"`
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	DurationMs int64  `json:"duration_ms"`
}

// SecurityRuleCheck represents individual CIS/Security benchmark check
type SecurityRuleCheck struct {
	RuleID      string `json:"rule_id"`
	Title       string `json:"title"`
	Severity    string `json:"severity"` // "CRITICAL", "HIGH", "MEDIUM", "LOW", "PASS"
	Status      string `json:"status"`   // "PASS", "FAIL"
	CurrentVal  string `json:"current_value"`
	ExpectedVal string `json:"expected_value"`
	Remediation string `json:"remediation"`
}

// SecurityReport contains the full security check results
type SecurityReport struct {
	Score       int                 `json:"score"` // 0-100
	AssessedAt  time.Time           `json:"assessed_at"`
	Rules       []SecurityRuleCheck `json:"rules"`
	FailedCount int                 `json:"failed_count"`
	WarnCount   int                 `json:"warn_count"`
	InfoCount   int                 `json:"info_count"`
	PassCount   int                 `json:"pass_count"`
}

// NodeStatus is the heartbeat payload
type NodeStatus struct {
	AgentVersion string        `json:"agent_version"`
	Status       string        `json:"status"` // "healthy", "degraded"
	// Reasons explains a degraded status.
	Reasons []string `json:"reasons,omitempty"`
	AgentUptime  int64         `json:"agent_uptime_sec"`
	Metrics      SystemMetrics `json:"metrics"`
}

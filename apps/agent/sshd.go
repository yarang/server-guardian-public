package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

var candidateConfigPaths = []string{
	"/etc/ssh/sshd_config",
	"/etc/sshd_config",
	"/usr/local/etc/ssh/sshd_config",
}

// FindSSHDConfig finds the primary sshd_config path
func FindSSHDConfig() string {
	for _, p := range candidateConfigPaths {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return "/etc/ssh/sshd_config" // default fallback
}

// sshdKeys are the directives the audit reads; everything else in the config is ignored.
var sshdKeys = map[string]bool{
	"port": true, "permitrootlogin": true, "passwordauthentication": true, "pubkeyauthentication": true,
	"maxauthtries": true, "x11forwarding": true, "allowtcpforwarding": true,
}

// runSSHD runs sshd as root: directly when the agent is root, otherwise through `sudo -n`
// (the hub-sudo option). It fails fast when neither is possible. Tests replace it.
var runSSHD = func(args ...string) ([]byte, error) {
	if os.Geteuid() == 0 {
		return exec.Command("sshd", args...).CombinedOutput()
	}
	return exec.Command("sudo", append([]string{"-n", "sshd"}, args...)...).CombinedOutput()
}

// defaultSSHConfigSummary holds the values modern OpenSSH uses when a directive is absent.
func defaultSSHConfigSummary(path string) SSHConfigSummary {
	return SSHConfigSummary{
		ConfigPath:         path,
		Port:               22,
		PermitRootLogin:    "prohibit-password",
		PasswordAuth:       "yes",
		PubkeyAuth:         "yes",
		MaxAuthTries:       6,
		X11Forwarding:      "no",
		AllowTcpForwarding: "yes",
	}
}

// SummaryFromEffectiveConfig builds a summary from `sshd -T` output (the effective config
// after Include and defaults). sshd only prints it when the config parsed, so the syntax
// counts as checked and valid. It serves the hub itself and output captured on fleet nodes.
func SummaryFromEffectiveConfig(text, path string) SSHConfigSummary {
	summary := defaultSSHConfigSummary(path)
	values := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		if k, v, ok := strings.Cut(strings.TrimSpace(line), " "); ok && sshdKeys[k] {
			if _, seen := values[k]; !seen {
				values[k] = strings.ToLower(strings.TrimSpace(v))
			}
		}
	}
	applySSHDValues(&summary, values)
	summary.Source = "sshd -T"
	summary.SyntaxChecked = true
	summary.IsValid = true
	summary.SyntaxMessage = "Configuration syntax is OK (sshd -T)."
	return summary
}

// ParseSSHDConfig reports the settings sshd actually applies on this host. With root it asks
// sshd itself (`sshd -T`: the effective configuration after Include and defaults, which also
// checks the syntax). Without root it reads the files the way sshd does (readSSHDConfig).
// sshd needs the root-only host keys to test a config, so the syntax stays unchecked
// (SyntaxChecked false) rather than being reported as invalid.
func ParseSSHDConfig(path string) (SSHConfigSummary, error) {
	if path == "" {
		path = FindSSHDConfig()
	}
	out, err := runSSHD("-T", "-f", path)
	if err == nil {
		return SummaryFromEffectiveConfig(string(out), path), nil
	}
	summary := defaultSSHConfigSummary(path)
	if msg := strings.TrimSpace(string(out)); isSSHDConfigError(msg) {
		// sshd ran as root and rejected the file: a real syntax error.
		summary.SyntaxChecked = true
		summary.IsValid = false
		summary.SyntaxMessage = msg
	}

	values := map[string]string{}
	lines, err := readSSHDConfig(osConfigFS{}, path, values, 0, nil)
	if err != nil {
		summary.Source = "unreadable"
		summary.SyntaxChecked = true
		summary.IsValid = false
		summary.SyntaxMessage = fmt.Sprintf("Config not readable: %v", err)
		return summary, nil
	}
	applySSHDValues(&summary, values)
	summary.RawLinesCount = lines
	summary.Source = "config files"
	if !summary.SyntaxChecked {
		summary.SyntaxMessage = "Not checked: sshd needs root to test the config (enable hub sudo or run the agent as root)."
	}
	return summary, nil
}

// ParseSSHDConfigText reads captured config file contents (path -> text) the way sshd reads
// files on disk, starting at mainPath. It is the fallback for fleet nodes where `sshd -T`
// could not run. It returns the Include patterns it followed so the caller can say which
// included files were not captured; the syntax is left unchecked.
func ParseSSHDConfigText(mainPath string, files map[string]string) (SSHConfigSummary, []string, error) {
	summary := defaultSSHConfigSummary(mainPath)
	values := map[string]string{}
	var includes []string
	lines, err := readSSHDConfig(mapConfigFS(files), mainPath, values, 0, &includes)
	if err != nil {
		return summary, includes, err
	}
	applySSHDValues(&summary, values)
	summary.RawLinesCount = lines
	summary.Source = "config files"
	summary.SyntaxMessage = "Not checked: sshd -T needs root on the node."
	return summary, includes, nil
}

// isSSHDConfigError tells a config error reported by sshd apart from sudo or sshd not being
// able to run at all.
func isSSHDConfigError(msg string) bool {
	lower := strings.ToLower(msg)
	if msg == "" || strings.Contains(lower, "sudo:") || strings.Contains(lower, "password is required") ||
		strings.Contains(lower, "no hostkeys available") || strings.Contains(lower, "not found") ||
		strings.Contains(lower, "no new privileges") {
		return false
	}
	return strings.Contains(lower, "bad configuration") || strings.Contains(lower, "line ") ||
		strings.Contains(lower, "unsupported option") || strings.Contains(lower, "terminating")
}

// sshdConfigFS is the file access readSSHDConfig needs, so the same reader serves files on
// this host and file contents captured on a remote node.
type sshdConfigFS interface {
	ReadFile(path string) ([]byte, error)
	Glob(pattern string) ([]string, error)
}

// osConfigFS reads the local filesystem.
type osConfigFS struct{}

func (osConfigFS) ReadFile(path string) ([]byte, error)  { return os.ReadFile(path) }
func (osConfigFS) Glob(pattern string) ([]string, error) { return filepath.Glob(pattern) }

// mapConfigFS serves captured file contents keyed by absolute path.
type mapConfigFS map[string]string

func (m mapConfigFS) ReadFile(path string) ([]byte, error) {
	if body, ok := m[path]; ok {
		return []byte(body), nil
	}
	return nil, fmt.Errorf("%s: %w", path, os.ErrNotExist)
}

func (m mapConfigFS) Glob(pattern string) ([]string, error) {
	var out []string
	for p := range m {
		ok, err := filepath.Match(pattern, p)
		if err != nil {
			return nil, err
		}
		if ok {
			out = append(out, p)
		}
	}
	return out, nil
}

// readSSHDConfig records the first value of each audited directive, following Include and
// skipping Match blocks, and returns the number of lines read. depth bounds Include loops.
// Followed Include patterns (made absolute) are appended to includes when it is non-nil.
func readSSHDConfig(fsys sshdConfigFS, path string, values map[string]string, depth int, includes *[]string) (int, error) {
	if depth > 8 {
		return 0, fmt.Errorf("include nesting too deep at %s", path)
	}
	data, err := fsys.ReadFile(path)
	if err != nil {
		return 0, err
	}
	lines := 0
	inMatch := false
	for _, raw := range strings.Split(string(data), "\n") {
		lines++
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Fields(strings.Replace(line, "=", " ", 1))
		if len(fields) < 2 {
			continue
		}
		key := strings.ToLower(fields[0])
		switch {
		case key == "match":
			// Everything up to the end of this file belongs to a conditional block.
			inMatch = true
		case inMatch:
		case key == "include":
			for _, pattern := range fields[1:] {
				if !filepath.IsAbs(pattern) {
					pattern = filepath.Join("/etc/ssh", pattern)
				}
				if includes != nil {
					*includes = append(*includes, pattern)
				}
				matches, _ := fsys.Glob(pattern)
				sort.Strings(matches)
				for _, m := range matches {
					n, err := readSSHDConfig(fsys, m, values, depth+1, includes)
					if err != nil {
						return lines, err
					}
					lines += n
				}
			}
		case sshdKeys[key]:
			if _, seen := values[key]; !seen {
				values[key] = strings.ToLower(fields[1])
			}
		}
	}
	return lines, nil
}

func applySSHDValues(summary *SSHConfigSummary, values map[string]string) {
	if v, ok := values["port"]; ok {
		if p, err := strconv.Atoi(v); err == nil {
			summary.Port = p
		}
	}
	if v, ok := values["maxauthtries"]; ok {
		if m, err := strconv.Atoi(v); err == nil {
			summary.MaxAuthTries = m
		}
	}
	for key, dst := range map[string]*string{
		"permitrootlogin":        &summary.PermitRootLogin,
		"passwordauthentication": &summary.PasswordAuth,
		"pubkeyauthentication":   &summary.PubkeyAuth,
		"x11forwarding":          &summary.X11Forwarding,
		"allowtcpforwarding":     &summary.AllowTcpForwarding,
	} {
		if v, ok := values[key]; ok {
			*dst = v
		}
	}
	// sshd -T prints the old spelling of prohibit-password.
	if summary.PermitRootLogin == "without-password" {
		summary.PermitRootLogin = "prohibit-password"
	}
}

// EvaluateSecurity runs CIS-style checks on SSH configuration
func EvaluateSecurity(cfg SSHConfigSummary) SecurityReport {
	report := SecurityReport{
		AssessedAt: time.Now(),
		Rules:      make([]SecurityRuleCheck, 0),
	}

	score := 100

	// Rule 1: Root Login
	rule1 := SecurityRuleCheck{
		RuleID:      "SEC-SSH-001",
		Title:       "Disable Direct Root Login via SSH",
		Severity:    "CRITICAL",
		CurrentVal:  cfg.PermitRootLogin,
		ExpectedVal: "no",
	}
	if cfg.PermitRootLogin == "no" {
		rule1.Status = "PASS"
		rule1.Severity = "PASS"
	} else if cfg.PermitRootLogin == "prohibit-password" || cfg.PermitRootLogin == "without-password" {
		rule1.Status = "WARN"
		rule1.Severity = "MEDIUM"
		rule1.Remediation = "Root login is restricted to keys, but disabling direct root login ('PermitRootLogin no') and using sudo is recommended."
		score -= 10
	} else {
		rule1.Status = "FAIL"
		rule1.Remediation = "Set 'PermitRootLogin no' in sshd_config to prevent brute-force attacks against root."
		score -= 30
	}
	report.Rules = append(report.Rules, rule1)

	// Rule 2: Password Authentication
	rule2 := SecurityRuleCheck{
		RuleID:      "SEC-SSH-002",
		Title:       "Enforce Key-Based Authentication Only",
		Severity:    "HIGH",
		CurrentVal:  cfg.PasswordAuth,
		ExpectedVal: "no",
	}
	if cfg.PasswordAuth == "no" {
		rule2.Status = "PASS"
		rule2.Severity = "PASS"
	} else {
		rule2.Status = "FAIL"
		rule2.Remediation = "Set 'PasswordAuthentication no' to eliminate dictionary and brute-force attacks. Ensure public keys are deployed first!"
		score -= 25
	}
	report.Rules = append(report.Rules, rule2)

	// Rule 3: Public Key Authentication
	rule3 := SecurityRuleCheck{
		RuleID:      "SEC-SSH-003",
		Title:       "Enable Public Key Authentication",
		Severity:    "HIGH",
		CurrentVal:  cfg.PubkeyAuth,
		ExpectedVal: "yes",
	}
	if cfg.PubkeyAuth == "yes" {
		rule3.Status = "PASS"
		rule3.Severity = "PASS"
	} else {
		rule3.Status = "FAIL"
		rule3.Remediation = "Set 'PubkeyAuthentication yes' to allow secure cryptographic key login."
		score -= 30
	}
	report.Rules = append(report.Rules, rule3)

	// Rule 4: Max Auth Tries
	rule4 := SecurityRuleCheck{
		RuleID:      "SEC-SSH-004",
		Title:       "Limit Maximum Authentication Attempts",
		Severity:    "MEDIUM",
		CurrentVal:  strconv.Itoa(cfg.MaxAuthTries),
		ExpectedVal: "<= 4",
	}
	if cfg.MaxAuthTries <= 4 && cfg.MaxAuthTries > 0 {
		rule4.Status = "PASS"
		rule4.Severity = "PASS"
	} else {
		rule4.Status = "FAIL"
		rule4.Remediation = "Set 'MaxAuthTries 3' or '4' to disconnect after repeated failures."
		score -= 10
	}
	report.Rules = append(report.Rules, rule4)

	// Rule 5: Non-default SSH Port
	rule5 := SecurityRuleCheck{
		RuleID:      "SEC-SSH-005",
		Title:       "Custom Non-standard SSH Port",
		Severity:    "LOW",
		CurrentVal:  strconv.Itoa(cfg.Port),
		ExpectedVal: "Non-22 (e.g. 2222)",
	}
	if cfg.Port != 22 {
		rule5.Status = "PASS"
		rule5.Severity = "PASS"
	} else {
		rule5.Status = "INFO"
		rule5.Remediation = "Using default port 22 attracts Internet port scanners. Consider custom port like 2222 or behind a firewall/VPN."
		score -= 5
	}
	report.Rules = append(report.Rules, rule5)

	// Only FAIL counts as failed; WARN and INFO are reported separately.
	for _, r := range report.Rules {
		switch r.Status {
		case "PASS":
			report.PassCount++
		case "FAIL":
			report.FailedCount++
		case "WARN":
			report.WarnCount++
		default:
			report.InfoCount++
		}
	}
	if score < 0 {
		score = 0
	}
	report.Score = score

	return report
}

// GetAuthorizedKeys reads ~/.ssh/authorized_keys
func GetAuthorizedKeys() ([]AuthorizedKey, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, err
	}
	authPath := filepath.Join(home, ".ssh", "authorized_keys")
	file, err := os.Open(authPath)
	if err != nil {
		return []AuthorizedKey{}, nil // empty if doesn't exist
	}
	defer file.Close()

	var keys []AuthorizedKey
	scanner := bufio.NewScanner(file)
	idx := 1
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, ok := parseAuthorizedKeyLine(line)
		if !ok {
			continue
		}
		k.ID = fmt.Sprintf("key-%d", idx)
		keys = append(keys, k)
		idx++
	}
	return keys, nil
}

// keyFingerprint matches `ssh-keygen -lf`: SHA256 over the decoded key blob, unpadded base64.
func keyFingerprint(pubKey string) string {
	blob, err := base64.StdEncoding.DecodeString(pubKey)
	if err != nil {
		return "invalid key encoding"
	}
	h := sha256.Sum256(blob)
	return "SHA256:" + base64.RawStdEncoding.EncodeToString(h[:])
}

// parseAuthorizedKeyLine splits "[options] type base64 [comment]". Options come first and may
// contain quoted spaces (from="a, b",command="x y"), so the key starts at the first field that
// is a key type.
func parseAuthorizedKeyLine(line string) (AuthorizedKey, bool) {
	var fields []string
	var cur strings.Builder
	inQuote := false
	for _, r := range line {
		switch {
		case r == '"':
			inQuote = !inQuote
			cur.WriteRune(r)
		case (r == ' ' || r == '\t') && !inQuote:
			if cur.Len() > 0 {
				fields = append(fields, cur.String())
				cur.Reset()
			}
		default:
			cur.WriteRune(r)
		}
	}
	if cur.Len() > 0 {
		fields = append(fields, cur.String())
	}
	for i := 0; i+1 < len(fields); i++ {
		if isKeyType(fields[i]) {
			return AuthorizedKey{
				Options:     strings.Join(fields[:i], " "),
				KeyType:     fields[i],
				PublicKey:   fields[i+1],
				Comment:     strings.Join(fields[i+2:], " "),
				Fingerprint: keyFingerprint(fields[i+1]),
			}, true
		}
	}
	return AuthorizedKey{}, false
}

func isKeyType(s string) bool {
	for _, p := range []string{"ssh-", "ecdsa-sha2-", "sk-ssh-", "sk-ecdsa-sha2-"} {
		if strings.HasPrefix(s, p) {
			return true
		}
	}
	return false
}

package main

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func stubSSHD(t *testing.T, out string, err error) {
	t.Helper()
	orig := runSSHD
	runSSHD = func(args ...string) ([]byte, error) { return []byte(out), err }
	t.Cleanup(func() { runSSHD = orig })
}

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestParseSSHDConfigUsesEffectiveConfigWithRoot(t *testing.T) {
	stubSSHD(t, "port 2222\npermitrootlogin without-password\npasswordauthentication no\nmaxauthtries 3\nciphers x\n", nil)
	cfg, _ := ParseSSHDConfig("/nonexistent/sshd_config")
	if cfg.Source != "sshd -T" || !cfg.SyntaxChecked || !cfg.IsValid {
		t.Fatalf("source %q checked %v valid %v", cfg.Source, cfg.SyntaxChecked, cfg.IsValid)
	}
	if cfg.Port != 2222 || cfg.PermitRootLogin != "prohibit-password" || cfg.PasswordAuth != "no" || cfg.MaxAuthTries != 3 {
		t.Fatalf("unexpected values: %+v", cfg)
	}
}

// Without root, the files are read like sshd reads them: first value wins, Include is
// followed (relative to the including file's directory rules), Match blocks are skipped.
func TestParseSSHDConfigFileFallback(t *testing.T) {
	stubSSHD(t, "sshd: no hostkeys available -- exiting.", errors.New("exit 1"))
	dir := t.TempDir()
	main := filepath.Join(dir, "sshd_config")
	writeFile(t, filepath.Join(dir, "conf.d", "50-cloud.conf"), "PasswordAuthentication no\nMaxAuthTries 3\n")
	writeFile(t, filepath.Join(dir, "conf.d", "90-late.conf"), "PasswordAuthentication yes\n")
	writeFile(t, main, ""+
		"# comment\n"+
		"Include "+filepath.Join(dir, "conf.d", "*.conf")+"\n"+
		"PasswordAuthentication yes\n"+
		"PermitRootLogin=no\n"+
		"PermitRootLogin yes\n"+
		"Match User backup\n"+
		"    X11Forwarding yes\n"+
		"    Port 2200\n")

	cfg, _ := ParseSSHDConfig(main)
	if cfg.Source != "config files" || cfg.SyntaxChecked {
		t.Fatalf("source %q checked %v", cfg.Source, cfg.SyntaxChecked)
	}
	if cfg.PasswordAuth != "no" {
		t.Errorf("PasswordAuthentication = %q, want the first value (from the include)", cfg.PasswordAuth)
	}
	if cfg.MaxAuthTries != 3 || cfg.PermitRootLogin != "no" {
		t.Errorf("MaxAuthTries %d PermitRootLogin %q", cfg.MaxAuthTries, cfg.PermitRootLogin)
	}
	if cfg.X11Forwarding != "no" || cfg.Port != 22 {
		t.Errorf("Match block leaked into global settings: X11 %q Port %d", cfg.X11Forwarding, cfg.Port)
	}
}

func TestParseSSHDConfigReportsRealSyntaxErrors(t *testing.T) {
	stubSSHD(t, "/etc/ssh/sshd_config line 3: Bad configuration option: Bogus\n/etc/ssh/sshd_config: terminating, 1 bad configuration options", errors.New("exit 255"))
	path := filepath.Join(t.TempDir(), "sshd_config")
	writeFile(t, path, "Bogus yes\n")
	cfg, _ := ParseSSHDConfig(path)
	if !cfg.SyntaxChecked || cfg.IsValid {
		t.Fatalf("checked %v valid %v msg %q", cfg.SyntaxChecked, cfg.IsValid, cfg.SyntaxMessage)
	}
}

func TestParseSSHDConfigUncheckedWithoutRoot(t *testing.T) {
	for _, out := range []string{"sudo: a password is required", "sudo: The \"no new privileges\" flag is set", "sshd: no hostkeys available -- exiting."} {
		stubSSHD(t, out, errors.New("exit 1"))
		path := filepath.Join(t.TempDir(), "sshd_config")
		writeFile(t, path, "Port 22\n")
		if cfg, _ := ParseSSHDConfig(path); cfg.SyntaxChecked {
			t.Errorf("%q reported as a checked config (valid=%v)", out, cfg.IsValid)
		}
	}
}

func TestEvaluateSecurityCountsOnlyFailuresAsFailed(t *testing.T) {
	r := EvaluateSecurity(SSHConfigSummary{Port: 22, PermitRootLogin: "prohibit-password", PasswordAuth: "yes", PubkeyAuth: "yes", MaxAuthTries: 6})
	if r.FailedCount != 2 || r.WarnCount != 1 || r.InfoCount != 1 || r.PassCount != 1 {
		t.Fatalf("fail %d warn %d info %d pass %d", r.FailedCount, r.WarnCount, r.InfoCount, r.PassCount)
	}
}

func TestKeyFingerprintMatchesSSHKeygen(t *testing.T) {
	// ssh-keygen -lf on this key prints SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
	const key = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
	if got, want := keyFingerprint(key), "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"; got != want {
		t.Fatalf("fingerprint = %s, want %s", got, want)
	}
}

func TestParseAuthorizedKeyLine(t *testing.T) {
	const blob = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
	cases := []struct{ line, options, comment string }{
		{"ssh-ed25519 " + blob + " me@laptop", "", "me@laptop"},
		{`from="100.64.0.7",no-agent-forwarding,no-port-forwarding ssh-ed25519 ` + blob + " guardian-hub-fleet@hub", `from="100.64.0.7",no-agent-forwarding,no-port-forwarding`, "guardian-hub-fleet@hub"},
		{`command="echo a b",from="10.0.0.1, 10.0.0.2" ssh-ed25519 ` + blob, `command="echo a b",from="10.0.0.1, 10.0.0.2"`, ""},
	}
	for _, c := range cases {
		k, ok := parseAuthorizedKeyLine(c.line)
		if !ok || k.KeyType != "ssh-ed25519" || k.Options != c.options || k.Comment != c.comment {
			t.Errorf("%q -> ok %v %+v", c.line, ok, k)
		}
		if k.Fingerprint != "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" {
			t.Errorf("%q fingerprint %s", c.line, k.Fingerprint)
		}
	}
	if _, ok := parseAuthorizedKeyLine("no key here"); ok {
		t.Error("line without a key type accepted")
	}
}

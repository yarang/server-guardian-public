package main

import (
	"strings"
	"testing"
)

func TestClassifySSH(t *testing.T) {
	cases := []struct {
		name   string
		code   int
		stderr string
		want   string
	}{
		{"success", 0, "", reachOK},
		{"success with warning", 0, "Warning: Permanently added", reachOK},
		{"timeout kill", -1, "", reachUnreachable},
		{"refused", 255, "ssh: connect to host h port 22: Connection refused", reachUnreachable},
		{"no route", 255, "ssh: connect to host h port 22: No route to host", reachUnreachable},
		{"bare 255", 255, "", reachUnreachable},
		{"key rejected", 255, "ubuntu@h: Permission denied (publickey).", reachAuthFailed},
		{"key changed", 255, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", reachHostKeyChange},
		{"strict unknown key", 255, "Host key verification failed.", reachHostKeyChange},
		{"remote command failed", 1, "", reachCommandFailed},
		// A remote command's own "Permission denied" must not read as an ssh auth failure.
		{"remote permission denied", 1, "cat: /root/x: Permission denied", reachCommandFailed},
		{"remote exit 127", 127, "bash: free: command not found", reachCommandFailed},
	}
	for _, c := range cases {
		if got := classifySSH(c.code, c.stderr); got != c.want {
			t.Errorf("%s: classifySSH(%d, %q) = %s, want %s", c.name, c.code, c.stderr, got, c.want)
		}
	}
}

// Diagnose, exec, the stream, the SSH audit and the node probe all report the same vocabulary for
// the same ssh outcome.
func TestReachabilityAgreesAcrossViews(t *testing.T) {
	writeServers(t, `[{"id":"a","name":"n-a"}]`)
	old := streamSSHExec
	t.Cleanup(func() { streamSSHExec = old })
	streamSSHExec = func(string, string, int) (int, string, string, int64) {
		return 255, "", "a: Permission denied (publickey).", 1
	}
	var streamed string
	if err := StreamExecFleet("id", 1, nil, func(e FleetStreamEvent) {
		if e.Type == "node_done" {
			streamed = e.Result.Reachability
		}
	}); err != nil {
		t.Fatal(err)
	}
	if streamed != reachAuthFailed {
		t.Errorf("stream reachability = %q", streamed)
	}

	srv := FleetNode{ID: "a", Name: "n-a"}
	for _, c := range []struct {
		code   int
		stderr string
		want   string
	}{
		{255, "Host key verification failed.", reachHostKeyChange},
		{255, "ssh: connect to host a port 22: Connection timed out", reachUnreachable},
		{-1, "", reachUnreachable},
		{0, "", reachCommandFailed}, // exit 0 without probe markers is not a successful audit
	} {
		if got := auditFromProbe(srv, c.code, "", c.stderr, 1).Reachability; got != c.want {
			t.Errorf("audit(%d, %q) = %q, want %q", c.code, c.stderr, got, c.want)
		}
	}
	if got := auditFromProbe(srv, 0, "", "", 1); strings.TrimSpace(got.Reachability) == "" {
		t.Error("audit reachability must always be set")
	}
}

func TestProbeNodeReportsReachability(t *testing.T) {
	old := sshProbe
	t.Cleanup(func() { sshProbe = old })
	for _, c := range []struct {
		code          int
		stderr        string
		status, reach string
	}{
		{0, "", "ok", reachOK},
		{255, "Permission denied (publickey).", "key_not_installed", reachAuthFailed},
		{255, "Host key verification failed.", "unreachable", reachHostKeyChange},
		{255, "Connection timed out", "unreachable", reachUnreachable},
	} {
		sshProbe = func(string, string, int) (int, string, string, int64) { return c.code, "x86_64\n", c.stderr, 1 }
		got := probeNode("n1")
		if got.Status != c.status || got.Reachability != c.reach {
			t.Errorf("probe(%d, %q) = %s/%s, want %s/%s", c.code, c.stderr, got.Status, got.Reachability, c.status, c.reach)
		}
	}
}

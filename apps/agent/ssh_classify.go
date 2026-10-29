package main

import "strings"

// Reachability values shared by fleet diagnose, fleet exec (including the stream), the SSH audit
// and the node registration probe, so one node never reads "OFFLINE" on one screen and "ok" on
// another. They describe what happened to the ssh connection, not to the remote command.
const (
	reachOK            = "ok"               // ssh connected and the remote command exited 0
	reachAuthFailed    = "auth_failed"      // the node rejected the fleet key (not installed, wrong from=)
	reachHostKeyChange = "host_key_changed" // host key verification failed (changed or unpinned under strict checking)
	reachUnreachable   = "unreachable"      // no connection: timeout, refused, no route, DNS, ssh killed
	reachCommandFailed = "command_failed"   // ssh connected, the remote command exited non-zero
)

// sshConnectFailure is ssh's own exit status for a failure before or outside the remote command.
const sshConnectFailure = 255

// classifySSH maps an ssh run (exit code and stderr) to one reachability value.
//
// Exit 0 is ok. -1 is the timeout kill from executeSSH, so the node counts as unreachable. Exit 255
// is ssh itself failing; stderr then tells a host key problem and a rejected key apart from a plain
// connection failure. Any other non-zero exit came from the remote command, which means ssh
// connected and authenticated: stderr text such as "Permission denied" is then the command's own
// output and must not be read as an ssh failure.
func classifySSH(exitCode int, stderr string) string {
	switch {
	case exitCode == 0:
		return reachOK
	case exitCode < 0:
		return reachUnreachable
	case exitCode != sshConnectFailure:
		return reachCommandFailed
	}
	switch {
	case strings.Contains(stderr, "REMOTE HOST IDENTIFICATION HAS CHANGED"),
		strings.Contains(stderr, "Host key verification failed"):
		return reachHostKeyChange
	case strings.Contains(stderr, "Permission denied"):
		return reachAuthFailed
	}
	return reachUnreachable
}

package main

import (
	"errors"
	"net/http"
	"os"
	"regexp"
	"strings"
)

// errHubNotTarget is returned when a command would run on the hub through the fleet (ssh) path
// while hub sudo is off. The hub is in the fleet list (the agent reaches it over the fleet key),
// so ssh-ing to it would run commands outside the agent's sandbox and bypass the sudo restriction.
var errHubNotTarget = errors.New("the hub is not a fleet target while hub sudo is off; use /api/exec")

var noNewPrivsRe = regexp.MustCompile(`(?m)^NoNewPrivs:\s*(\d+)`)

// readProcStatus reads /proc/self/status; tests replace it.
var readProcStatus = func() ([]byte, error) { return os.ReadFile("/proc/self/status") }

// hubSudoOff reports whether the hub is installed without the hub-sudo option.
//
// scripts/install-agent-service.sh --hub-sudo installs the drop-in hub-sudo.conf, which sets
// NoNewPrivileges=no; --no-hub-sudo keeps the base unit's NoNewPrivileges=yes. The agent can
// observe that on itself: /proc/self/status has "NoNewPrivs: 1" exactly when the sandbox is on.
// GUARDIAN_HUB_SUDO=on|off overrides the observation (for tests and unusual setups). Without
// /proc (development on macOS) the answer is "not off", so nothing is refused.
func hubSudoOff() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("GUARDIAN_HUB_SUDO"))) {
	case "off":
		return true
	case "on":
		return false
	}
	data, err := readProcStatus()
	if err != nil {
		return false
	}
	m := noNewPrivsRe.FindSubmatch(data)
	return m != nil && string(m[1]) == "1"
}

// isHubNode reports whether s is the hub's own inventory entry.
func isHubNode(s FleetNode) bool { return s.GuardianRole == "hub" }

// hubIDIn reports whether id is the inventory id of the hub entry in nodes.
func hubIDIn(nodes []FleetNode, id string) bool {
	for _, s := range nodes {
		if s.ID == id && isHubNode(s) {
			return true
		}
	}
	return false
}

// execNodes is selectNodes for the command routes plus the hub guard. When hub sudo is off an
// explicit target that names the hub (id or name) is refused with errHubNotTarget; an empty target
// list (the whole fleet) simply leaves the hub out.
func execNodes(all []FleetNode, targets []string) ([]FleetNode, error) {
	nodes, unknown := selectNodes(all, targets)
	if len(unknown) > 0 {
		return nil, unknownTargetsError(unknown)
	}
	if !hubSudoOff() {
		return nodes, nil
	}
	out := make([]FleetNode, 0, len(nodes))
	for _, s := range nodes {
		if isHubNode(s) {
			if len(targets) > 0 {
				return nil, errHubNotTarget
			}
			continue
		}
		out = append(out, s)
	}
	return out, nil
}

// execErrorStatus is fleetErrorStatus plus the 409 of the hub guard.
func execErrorStatus(err error) int {
	if errors.Is(err, errHubNotTarget) {
		return http.StatusConflict
	}
	return fleetErrorStatus(err)
}

// isRejection reports whether err means the request was refused before anything ran.
func isRejection(err error) bool {
	return errors.Is(err, errUnknownHost) || errors.Is(err, errHubNotTarget)
}

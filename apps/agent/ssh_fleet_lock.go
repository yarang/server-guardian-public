package main

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// fleetLockTimeout bounds how long a dashboard write waits for scripts/setup-hub-ssh.sh (or another
// agent process) to finish rewriting the fleet file.
var fleetLockTimeout = 10 * time.Second

const fleetLockPoll = 50 * time.Millisecond

// fleetLockPath is the advisory lock file next to the fleet file. scripts/setup-hub-ssh.sh takes
// the same lock (flock on this path) around its read-modify-write of the fleet file.
func fleetLockPath() string { return fleetFilePath() + ".lock" }

// lockFleet serialises a fleet-file read-modify-write across handlers (fleetFileMu) and across
// processes (advisory flock on fleetLockPath). The returned func releases both. An unreachable lock
// is an error rather than a silent unlocked write, because a lost update leaves a node with an
// inventory entry and a pinned key but no Host block.
func lockFleet() (func(), error) {
	fleetFileMu.Lock()
	path := fleetLockPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		fleetFileMu.Unlock()
		return nil, &apiError{Status: http.StatusInternalServerError, Code: "fleet_lock", Msg: "cannot prepare the fleet lock: " + err.Error()}
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		fleetFileMu.Unlock()
		return nil, &apiError{Status: http.StatusInternalServerError, Code: "fleet_lock", Msg: "cannot open the fleet lock file: " + err.Error()}
	}
	deadline := time.Now().Add(fleetLockTimeout)
	for {
		err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if err != syscall.EWOULDBLOCK && err != syscall.EINTR {
			_ = f.Close()
			fleetFileMu.Unlock()
			return nil, &apiError{Status: http.StatusInternalServerError, Code: "fleet_lock", Msg: fmt.Sprintf("cannot lock %s: %v", path, err)}
		}
		if time.Now().After(deadline) {
			_ = f.Close()
			fleetFileMu.Unlock()
			return nil, &apiError{Status: http.StatusConflict, Code: "fleet_busy",
				Msg:  "another process (for example scripts/setup-hub-ssh.sh) is rewriting the fleet file",
				Hint: "retry in a moment"}
		}
		time.Sleep(fleetLockPoll)
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
		fleetFileMu.Unlock()
	}, nil
}

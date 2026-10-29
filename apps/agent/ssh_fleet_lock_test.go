package main

import (
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// holdFleetLock takes the lock the way scripts/setup-hub-ssh.sh does (a separate open file
// description, so it conflicts with the agent's flock even inside one process).
func holdFleetLock(t *testing.T) func() {
	t.Helper()
	f, err := os.OpenFile(fleetLockPath(), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	return func() { _ = f.Close() }
}

func TestFleetLockIsNextToFleetFile(t *testing.T) {
	t.Setenv("GUARDIAN_SSH_FLEET_FILE", filepath.Join(t.TempDir(), "sub", "guardian_fleet.conf"))
	if got, want := fleetLockPath(), fleetFilePath()+".lock"; got != want {
		t.Fatalf("lock path %q, want %q", got, want)
	}
	unlock, err := lockFleet()
	if err != nil {
		t.Fatal(err)
	}
	unlock()
	if _, err := os.Stat(fleetLockPath()); err != nil {
		t.Errorf("lock file not created: %v", err)
	}
}

func TestFleetLockWaitsForAnotherProcess(t *testing.T) {
	t.Setenv("GUARDIAN_SSH_FLEET_FILE", filepath.Join(t.TempDir(), "guardian_fleet.conf"))
	old := fleetLockTimeout
	t.Cleanup(func() { fleetLockTimeout = old })

	// A holder that never lets go: the writer gives up with a retryable 409.
	fleetLockTimeout = 150 * time.Millisecond
	release := holdFleetLock(t)
	_, err := lockFleet()
	var ae *apiError
	if !errors.As(err, &ae) || ae.Status != http.StatusConflict || ae.Code != "fleet_busy" {
		t.Fatalf("busy lock: %v", err)
	}
	// The in-process mutex was released on failure, so a later write is not wedged.
	release()

	// A holder that lets go in time: the writer proceeds.
	fleetLockTimeout = 5 * time.Second
	release = holdFleetLock(t)
	go func() {
		time.Sleep(200 * time.Millisecond)
		release()
	}()
	unlock, err := lockFleet()
	if err != nil {
		t.Fatalf("lock after release: %v", err)
	}
	unlock()
}

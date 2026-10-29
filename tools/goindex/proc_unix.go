//go:build unix

package main

import (
	"os/exec"
	"syscall"
)

// ownProcessGroup puts the command in its own process group and kills the whole group on cancel, so a timeout
// also stops the compilers that `go list -export` started.
func ownProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
}

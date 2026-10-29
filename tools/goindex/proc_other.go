//go:build !unix

package main

import "os/exec"

func ownProcessGroup(cmd *exec.Cmd) {}

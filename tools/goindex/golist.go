package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
)

// forcedEnv are the settings every go command of the helper runs with, whatever the caller's environment says:
// no C compiler, no toolchain download, no dependency download, no go.work surprises. GOCACHE is the caller's.
var forcedEnv = []string{
	"CGO_ENABLED=0",
	"GOTOOLCHAIN=local",
	"GOFLAGS=-mod=readonly",
	"GOPROXY=off",
	"GOWORK=off",
}

// childEnv is base with forcedEnv applied (earlier values of the same names are dropped).
func childEnv(base []string) []string {
	forced := map[string]bool{}
	for _, kv := range forcedEnv {
		forced[kv[:strings.IndexByte(kv, '=')]] = true
	}
	env := make([]string, 0, len(base)+len(forcedEnv))
	for _, kv := range base {
		if name, _, ok := strings.Cut(kv, "="); ok && forced[name] {
			continue
		}
		env = append(env, kv)
	}
	return append(env, forcedEnv...)
}

type listedModule struct {
	Path      string
	Main      bool
	Dir       string
	GoVersion string
}

type listedError struct {
	Pos string
	Err string
}

// listedPackage is the part of `go list -json` the helper reads.
type listedPackage struct {
	ImportPath string
	Name       string
	Dir        string
	Export     string
	ForTest    string
	GoFiles    []string
	Imports    []string
	ImportMap  map[string]string
	Module     *listedModule
	DepOnly    bool
	Error      *listedError
}

// goRunner runs go commands with the forced environment.
type goRunner struct {
	bin string
	env []string
}

func newGoRunner(bin string) *goRunner {
	if bin == "" {
		bin = "go"
	}
	return &goRunner{bin: bin, env: childEnv(os.Environ())}
}

func (g *goRunner) command(ctx context.Context, dir string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, g.bin, args...)
	cmd.Dir = dir
	cmd.Env = append(append([]string{}, g.env...), "PWD="+dir)
	ownProcessGroup(cmd)
	return cmd
}

// cappedBuffer keeps the first limit bytes of what is written to it.
type cappedBuffer struct {
	buf   bytes.Buffer
	limit int
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	if room := c.limit - c.buf.Len(); room > 0 {
		c.buf.Write(p[:min(room, len(p))])
	}
	return len(p), nil
}

// env returns the go environment values (GOVERSION, GOOS, GOARCH) as reported by the toolchain in use.
func (g *goRunner) env3(ctx context.Context, dir string) (goVersion, goos, goarch string, err error) {
	out, err := g.command(ctx, dir, "env", "GOVERSION", "GOOS", "GOARCH").Output()
	if err != nil {
		return "", "", "", fmt.Errorf("go env: %w", err)
	}
	fields := strings.Fields(string(out))
	if len(fields) != 3 {
		return "", "", "", fmt.Errorf("go env: unexpected output %q", strings.TrimSpace(string(out)))
	}
	return fields[0], fields[1], fields[2], nil
}

// list runs `go list -e -json -export -deps -test ./...` in dir and returns the packages in the order go prints
// them (dependencies before dependents). A non-zero exit with parsable output is not an error: -e reports problems
// per package and the caller reads them from the packages. The first stderr line comes back for module-level messages.
func (g *goRunner) list(ctx context.Context, dir string) ([]*listedPackage, string, error) {
	cmd := g.command(ctx, dir, "list", "-e", "-json", "-export", "-deps", "-test", "-buildvcs=false", "./...")
	stderr := &cappedBuffer{limit: 8192}
	cmd.Stderr = stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, "", err
	}
	if err := cmd.Start(); err != nil {
		return nil, "", fmt.Errorf("go list: %w", err)
	}
	var pkgs []*listedPackage
	dec := json.NewDecoder(stdout)
	var decodeErr error
	for {
		pkg := new(listedPackage)
		if err := dec.Decode(pkg); err != nil {
			if !errors.Is(err, io.EOF) {
				decodeErr = err
			}
			break
		}
		pkgs = append(pkgs, pkg)
	}
	_, _ = io.Copy(io.Discard, stdout)
	waitErr := cmd.Wait()
	if ctxErr := ctx.Err(); ctxErr != nil {
		return nil, "", ctxErr
	}
	first := firstLine(stderr.buf.String())
	if decodeErr != nil {
		return nil, first, fmt.Errorf("go list output: %w", decodeErr)
	}
	if len(pkgs) == 0 {
		msg := first
		if msg == "" && waitErr != nil {
			msg = waitErr.Error()
		}
		return nil, first, fmt.Errorf("go list produced no packages: %s", msg)
	}
	return pkgs, first, nil
}

func firstLine(s string) string {
	for _, line := range strings.Split(s, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			if len(line) > 300 {
				line = line[:300]
			}
			return line
		}
	}
	return ""
}

// basePath is the import path without a test-variant suffix: "p [p.test]" is "p".
func basePath(id string) string {
	if i := strings.Index(id, " ["); i >= 0 {
		return id[:i]
	}
	return id
}

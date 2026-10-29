package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Fleet SSH file: the hub's connection settings for registered nodes, one Host block per node
// in SSH config syntax (docs/design/node-registration.md). The agent only ever writes blocks
// of a fixed shape; everything else in the file is preserved byte for byte and never rewritten.

const (
	fleetIdentityFile  = "~/.ssh/guardian_fleet.pub"
	fleetIdentityAgent = "/run/guardian-fleet/agent.sock"
	// fleetKnownHostsRef is the literal UserKnownHostsFile of registered blocks. The real file is
	// knownHostsPath(); the two only differ when a test overrides GUARDIAN_SSH_KNOWN_HOSTS.
	fleetKnownHostsRef = "~/.ssh/guardian_known_hosts"
	fleetFileHeader    = "# Managed by Server Guardian (agent and scripts/setup-hub-ssh.sh).\n" +
		"# Host blocks of the fixed shape are rewritten by the agent; any other block is left alone.\n\n"
)

// fleetShape tells the two managed block shapes apart.
type fleetShape int

const (
	// shapeRegistered is written for nodes registered from the dashboard: the host key is pinned
	// in guardian_known_hosts under HostKeyAlias and StrictHostKeyChecking is yes.
	shapeRegistered fleetShape = iota + 1
	// shapeMigrated is the block scripts/setup-hub-ssh.sh writes: no pinned key, accept-new.
	shapeMigrated
)

func (s fleetShape) String() string {
	if s == shapeRegistered {
		return "registered"
	}
	return "migrated"
}

// sshHomeDir returns the home directory used to resolve default SSH paths.
func sshHomeDir() string {
	if home, err := os.UserHomeDir(); err == nil {
		return home
	}
	return ""
}

func envPathOr(env, rel string) string {
	if v := strings.TrimSpace(os.Getenv(env)); v != "" {
		return v
	}
	return filepath.Join(sshHomeDir(), rel)
}

// fleetFilePath is GUARDIAN_SSH_FLEET_FILE or ~/.ssh/guardian_fleet.conf.
func fleetFilePath() string { return envPathOr("GUARDIAN_SSH_FLEET_FILE", ".ssh/guardian_fleet.conf") }

// knownHostsPath is GUARDIAN_SSH_KNOWN_HOSTS or ~/.ssh/guardian_known_hosts.
func knownHostsPath() string {
	return envPathOr("GUARDIAN_SSH_KNOWN_HOSTS", ".ssh/guardian_known_hosts")
}

// sshConfigPath is GUARDIAN_SSH_CONFIG or ~/.ssh/config (read only: the agent never writes it).
func sshConfigPath() string { return envPathOr("GUARDIAN_SSH_CONFIG", ".ssh/config") }

// fleetPubKeyPath is GUARDIAN_FLEET_PUBKEY_FILE or ~/.ssh/guardian_fleet.pub.
func fleetPubKeyPath() string {
	return envPathOr("GUARDIAN_FLEET_PUBKEY_FILE", ".ssh/guardian_fleet.pub")
}

// fleetIncludeDirective is the single line the hub's ~/.ssh/config needs.
func fleetIncludeDirective() string { return "Include " + displayPath(fleetFilePath()) }

// displayPath shortens a path under the home directory to ~/..., the form ssh config uses.
func displayPath(p string) string {
	if home := sshHomeDir(); home != "" && strings.HasPrefix(p, home+string(filepath.Separator)) {
		return "~/" + filepath.ToSlash(strings.TrimPrefix(p, home+string(filepath.Separator)))
	}
	return p
}

// ---- parsing ----

// fleetBlock is one Host or Match section of the fleet file.
type fleetBlock struct {
	// Raw holds the section's exact bytes, from its Host/Match line to just before the next one.
	Raw string
	// Patterns are the Host line's patterns (nil for Match sections).
	Patterns []string
	Managed  bool
	Shape    fleetShape
	// Fields below are only set for managed blocks.
	Alias    string
	HostName string
	User     string
	Port     int
	// Trailer is the managed block's trailing blank and comment lines, kept when it is rewritten.
	Trailer string
}

// fleetFile is the parsed fleet file: text before the first block, then the blocks.
type fleetFile struct {
	Preamble string
	Blocks   []*fleetBlock
}

// sshDirective splits one SSH config line into its lower-cased keyword and arguments. Blank
// and comment lines report ok=false. Arguments may be double-quoted; "keyword=value" works.
func sshDirective(line string) (kw string, args []string, ok bool) {
	s := strings.TrimSpace(line)
	if s == "" || strings.HasPrefix(s, "#") {
		return "", nil, false
	}
	i := strings.IndexAny(s, " \t=")
	if i < 0 {
		return strings.ToLower(s), nil, true
	}
	kw = strings.ToLower(s[:i])
	rest := strings.TrimLeft(s[i:], " \t")
	rest = strings.TrimLeft(strings.TrimPrefix(rest, "="), " \t")
	var cur strings.Builder
	inQuote, have := false, false
	for _, r := range rest {
		switch {
		case r == '"':
			inQuote, have = !inQuote, true
		case (r == ' ' || r == '\t') && !inQuote:
			if have {
				args = append(args, cur.String())
				cur.Reset()
				have = false
			}
		default:
			cur.WriteRune(r)
			have = true
		}
	}
	if have {
		args = append(args, cur.String())
	}
	return kw, args, true
}

// parseFleetFile splits data into a preamble and Host/Match blocks and recognises managed
// blocks. Joining the preamble and every block's Raw reproduces data exactly.
func parseFleetFile(data string) *fleetFile {
	ff := &fleetFile{}
	var cur *fleetBlock
	var curLines []string
	flush := func() {
		if cur == nil {
			return
		}
		cur.Raw = strings.Join(curLines, "")
		classifyBlock(cur, curLines)
		ff.Blocks = append(ff.Blocks, cur)
		cur, curLines = nil, nil
	}
	for _, line := range strings.SplitAfter(data, "\n") {
		if line == "" {
			continue
		}
		if kw, args, ok := sshDirective(line); ok && (kw == "host" || kw == "match") {
			flush()
			cur = &fleetBlock{}
			if kw == "host" {
				cur.Patterns = args
			}
		}
		if cur == nil {
			ff.Preamble += line
			continue
		}
		curLines = append(curLines, line)
	}
	flush()
	return ff
}

// classifyBlock marks b managed when its lines match one of the two fixed shapes exactly.
func classifyBlock(b *fleetBlock, lines []string) {
	// Trailing blank and comment lines are not part of the shape.
	end := len(lines)
	for end > 1 {
		t := strings.TrimSpace(lines[end-1])
		if t != "" && !strings.HasPrefix(t, "#") {
			break
		}
		end--
	}
	body := make([]string, 0, end)
	for _, l := range lines[:end] {
		body = append(body, strings.TrimSuffix(l, "\n"))
	}
	if len(b.Patterns) != 1 || len(body) < 8 || body[0] != "Host "+b.Patterns[0] {
		return
	}
	alias := b.Patterns[0]
	if !validNodeID(alias) {
		return
	}
	host, ok1 := strings.CutPrefix(body[1], "    HostName ")
	user, ok2 := strings.CutPrefix(body[2], "    User ")
	portS, ok3 := strings.CutPrefix(body[3], "    Port ")
	port, err := strconv.Atoi(portS)
	if !ok1 || !ok2 || !ok3 || err != nil || strconv.Itoa(port) != portS ||
		validateHostName(host) != nil || validateUser(user) != nil || validatePort(port) != nil {
		return
	}
	fixed := []string{
		"    IdentityFile " + fleetIdentityFile,
		"    IdentityAgent " + fleetIdentityAgent,
		"    IdentitiesOnly yes",
	}
	rest := body[4:]
	if len(rest) < len(fixed) {
		return
	}
	for i, want := range fixed {
		if rest[i] != want {
			return
		}
	}
	rest = rest[len(fixed):]
	var shape fleetShape
	switch {
	case len(rest) == 1 && rest[0] == "    StrictHostKeyChecking accept-new":
		shape = shapeMigrated
	case len(rest) == 3 && rest[0] == "    StrictHostKeyChecking yes" &&
		rest[1] == "    UserKnownHostsFile "+fleetKnownHostsRef && rest[2] == "    HostKeyAlias "+alias:
		shape = shapeRegistered
	default:
		return
	}
	b.Managed, b.Shape = true, shape
	b.Alias, b.HostName, b.User, b.Port = alias, host, user, port
	b.Trailer = strings.Join(lines[end:], "")
}

// find returns the block whose Host line names alias (managed or not) and whether it is managed.
func (ff *fleetFile) find(alias string) (*fleetBlock, bool) {
	for _, b := range ff.Blocks {
		for _, p := range b.Patterns {
			if p == alias || p == "!"+alias {
				return b, b.Managed && b.Alias == alias
			}
		}
	}
	return nil, false
}

func (ff *fleetFile) managedByAlias() map[string]*fleetBlock {
	out := map[string]*fleetBlock{}
	for _, b := range ff.Blocks {
		if b.Managed {
			out[b.Alias] = b
		}
	}
	return out
}

// unmanagedAliases lists the Host patterns of blocks the agent leaves alone.
func (ff *fleetFile) unmanagedAliases() []string {
	var out []string
	for _, b := range ff.Blocks {
		if !b.Managed {
			out = append(out, b.Patterns...)
		}
	}
	return out
}

// ---- rendering ----

// renderFleetBlock renders the fixed block for a node. Inputs must already be validated.
func renderFleetBlock(alias, host, user string, port int, shape fleetShape) string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "Host %s\n    HostName %s\n    User %s\n    Port %d\n", alias, host, user, port)
	fmt.Fprintf(&sb, "    IdentityFile %s\n    IdentityAgent %s\n    IdentitiesOnly yes\n", fleetIdentityFile, fleetIdentityAgent)
	if shape == shapeRegistered {
		fmt.Fprintf(&sb, "    StrictHostKeyChecking yes\n    UserKnownHostsFile %s\n    HostKeyAlias %s\n", fleetKnownHostsRef, alias)
	} else {
		sb.WriteString("    StrictHostKeyChecking accept-new\n")
	}
	return sb.String()
}

// render joins the preamble and blocks back into file text.
func (ff *fleetFile) render() string {
	var sb strings.Builder
	sb.WriteString(ff.Preamble)
	for _, b := range ff.Blocks {
		sb.WriteString(b.Raw)
	}
	return sb.String()
}

// set replaces the managed block for alias, or appends a new one. Callers have already
// refused unmanaged blocks with the same alias.
func (ff *fleetFile) set(alias, host, user string, port int, shape fleetShape) {
	text := renderFleetBlock(alias, host, user, port, shape)
	if b, managed := ff.find(alias); managed {
		b.Raw = text + b.Trailer
		b.Shape, b.HostName, b.User, b.Port = shape, host, user, port
		return
	}
	// A new block goes after a blank line; the previous block must end with a newline.
	if n := len(ff.Blocks); n > 0 {
		last := ff.Blocks[n-1]
		if !strings.HasSuffix(last.Raw, "\n") {
			last.Raw += "\n"
		}
		if !strings.HasSuffix(last.Raw, "\n\n") {
			last.Raw += "\n"
		}
	} else if ff.Preamble == "" {
		ff.Preamble = fleetFileHeader
	} else if !strings.HasSuffix(ff.Preamble, "\n\n") {
		if !strings.HasSuffix(ff.Preamble, "\n") {
			ff.Preamble += "\n"
		}
		ff.Preamble += "\n"
	}
	ff.Blocks = append(ff.Blocks, parseFleetFile(text + "\n").Blocks[0])
}

// remove drops the managed block for alias. Comment lines after it are kept (they usually
// describe the next block).
func (ff *fleetFile) remove(alias string) bool {
	for i, b := range ff.Blocks {
		if b.Managed && b.Alias == alias {
			if strings.TrimSpace(b.Trailer) == "" {
				ff.Blocks = append(ff.Blocks[:i], ff.Blocks[i+1:]...)
			} else {
				b.Raw, b.Managed, b.Patterns = b.Trailer, false, nil
			}
			return true
		}
	}
	return false
}

// ---- Include detection ----

// includeStatus reports whether ~/.ssh/config pulls in the fleet file.
type includeStatus struct {
	// Present: some Include line names the fleet file (exact path or a matching glob).
	Present bool
	// Effective: that Include comes before the first Host/Match line, so it applies globally.
	Effective bool
}

// detectFleetInclude scans an ssh config for an Include of fleetPath. Relative include paths
// are relative to ~/.ssh, as ssh resolves them for a user config. Includes are not followed.
func detectFleetInclude(configData, fleetPath, home string) includeStatus {
	var st includeStatus
	seenHost := false
	for _, line := range strings.Split(configData, "\n") {
		kw, args, ok := sshDirective(line)
		if !ok {
			continue
		}
		switch kw {
		case "host", "match":
			seenHost = true
		case "include":
			for _, arg := range args {
				p := arg
				switch {
				case p == "~":
					p = home
				case strings.HasPrefix(p, "~/"):
					p = filepath.Join(home, p[2:])
				case !filepath.IsAbs(p):
					p = filepath.Join(home, ".ssh", p)
				}
				p = filepath.Clean(p)
				if p == filepath.Clean(fleetPath) {
					st.Present = true
				} else if m, err := filepath.Match(p, filepath.Clean(fleetPath)); err == nil && m {
					st.Present = true
				} else {
					continue
				}
				if !seenHost {
					st.Effective = true
				}
			}
		}
	}
	return st
}

// ---- files ----

// fleetFileMu serialises every read-modify-write of the fleet file, guardian_known_hosts and
// servers.json across handlers, so concurrent dashboard sessions cannot interleave edits.
var fleetFileMu sync.Mutex

// readFileIfExists returns the file's bytes, or nil with exists=false when it is missing.
func readFileIfExists(path string) (data []byte, exists bool, err error) {
	data, err = os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	return data, err == nil, err
}

// @MX:WARN: [AUTO] Replaces a file the ssh client trusts (fleet Host blocks, pinned host keys) or the node inventory.
// @MX:REASON: Content must come from validated inputs only. The write is atomic (temp file + rename, mode 600) and the previous content is kept as <path>.bak.
func writeFileAtomic(path string, data []byte, keepBackup bool) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	if keepBackup {
		if old, exists, err := readFileIfExists(path); err != nil {
			return err
		} else if exists {
			if err := replaceFile(path+".bak", old); err != nil {
				return fmt.Errorf("backup: %w", err)
			}
		}
	}
	return replaceFile(path, data)
}

func replaceFile(path string, data []byte) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	cleanup := func() { _ = tmp.Close(); _ = os.Remove(name) }
	if err := tmp.Chmod(0o600); err != nil {
		cleanup()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		cleanup()
		return err
	}
	if err := tmp.Sync(); err != nil {
		cleanup()
		return err
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(name)
		return err
	}
	if err := os.Rename(name, path); err != nil {
		_ = os.Remove(name)
		return err
	}
	return nil
}

// fileSnapshot remembers a file's content so a failed registration can put it back.
type fileSnapshot struct {
	path   string
	data   []byte
	exists bool
}

func snapshotFile(path string) (fileSnapshot, error) {
	data, exists, err := readFileIfExists(path)
	return fileSnapshot{path: path, data: data, exists: exists}, err
}

// restore writes the snapshot back, or removes the file if it did not exist before.
func (s fileSnapshot) restore() error {
	if !s.exists {
		if err := os.Remove(s.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		return nil
	}
	return replaceFile(s.path, s.data)
}

// ---- host keys ----

// hostKey is one public host key from ssh-keyscan.
type hostKey struct {
	Type        string `json:"type"`
	Fingerprint string `json:"fingerprint"`
	// Blob is the base64 key as it appears in known_hosts (not sent to clients).
	Blob string `json:"-"`
}

// fingerprintSHA256 is OpenSSH's SHA256 fingerprint: unpadded standard base64 of the SHA-256 of
// the raw key blob.
func fingerprintSHA256(blob []byte) string {
	sum := sha256.Sum256(blob)
	return "SHA256:" + base64.RawStdEncoding.EncodeToString(sum[:])
}

// blobKeyType reads the key type string a wire-format public key blob starts with.
func blobKeyType(blob []byte) (string, bool) {
	if len(blob) < 4 {
		return "", false
	}
	n := binary.BigEndian.Uint32(blob)
	if n == 0 || n > 64 || int(n) > len(blob)-4 {
		return "", false
	}
	return string(blob[4 : 4+n]), true
}

// parseKeyscan reads `host keytype base64` lines (ssh-keyscan stdout), skipping comments and any
// line whose key blob is malformed or whose declared type disagrees with the blob.
func parseKeyscan(out string) []hostKey {
	var keys []hostKey
	seen := map[string]bool{}
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) < 3 || strings.HasPrefix(f[0], "#") {
			continue
		}
		blob, err := base64.StdEncoding.DecodeString(f[2])
		if err != nil {
			continue
		}
		if typ, ok := blobKeyType(blob); !ok || typ != f[1] {
			continue
		}
		fp := fingerprintSHA256(blob)
		if seen[fp] {
			continue
		}
		seen[fp] = true
		keys = append(keys, hostKey{Type: f[1], Fingerprint: fp, Blob: f[2]})
	}
	return keys
}

// knownHostsWithout drops every line whose host field is exactly alias.
func knownHostsWithout(content, alias string) string {
	var sb strings.Builder
	for _, line := range strings.SplitAfter(content, "\n") {
		if line == "" {
			continue
		}
		f := strings.Fields(line)
		if len(f) > 0 && f[0] == alias {
			continue
		}
		sb.WriteString(line)
	}
	return sb.String()
}

// knownHostsWith replaces alias's lines with the single confirmed key.
func knownHostsWith(content, alias string, key hostKey) string {
	out := knownHostsWithout(content, alias)
	if out != "" && !strings.HasSuffix(out, "\n") {
		out += "\n"
	}
	return out + alias + " " + key.Type + " " + key.Blob + "\n"
}

// ---- command runner ----

// cmdResult is the outcome of an external command.
type cmdResult struct {
	Exit   int
	Stdout string
	Stderr string
	// Err is set when the command could not run at all (missing binary, timeout).
	Err error
}

// @MX:WARN: [AUTO] Runs ssh-keyscan and ssh -G on the hub with arguments built from request input.
// @MX:REASON: Every argument must be validated first (validateConnection): hosts never start with "-", and ssh gets "--" before the alias. No shell is involved.
func runExternal(name string, args []string, timeout time.Duration) cmdResult {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	res := cmdResult{Stdout: stdout.String(), Stderr: stderr.String()}
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			res.Exit = ee.ExitCode()
		} else {
			res.Exit, res.Err = -1, err
		}
	}
	return res
}

// commandRunner and sshProbe are package-level so tests never start a real ssh.
var (
	commandRunner = runExternal
	sshProbe      = executeSSH
)

// effectiveSSHConfig runs `ssh -G -- alias` and returns the lower-cased keys ssh resolved.
func effectiveSSHConfig(alias string) (map[string]string, error) {
	res := commandRunner("ssh", []string{"-G", "--", alias}, 10*time.Second)
	if res.Err != nil || res.Exit != 0 {
		msg := strings.TrimSpace(res.Stderr)
		if res.Err != nil {
			msg = res.Err.Error()
		}
		return nil, fmt.Errorf("ssh -G failed: %s", msg)
	}
	cfg := map[string]string{}
	for _, line := range strings.Split(res.Stdout, "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), " ")
		if ok {
			if _, dup := cfg[k]; !dup {
				cfg[k] = strings.TrimSpace(v)
			}
		}
	}
	return cfg, nil
}

// verifyEffective checks that ssh resolves alias to exactly the intended connection.
func verifyEffective(alias, host, user string, port int, shape fleetShape) error {
	cfg, err := effectiveSSHConfig(alias)
	if err != nil {
		return err
	}
	if cfg["hostname"] != host || cfg["user"] != user || cfg["port"] != strconv.Itoa(port) {
		return fmt.Errorf("ssh resolves %s to %s@%s:%s, expected %s@%s:%d (is the fleet file included before other Host blocks?)",
			alias, cfg["user"], cfg["hostname"], cfg["port"], user, host, port)
	}
	if shape == shapeRegistered && cfg["hostkeyalias"] != alias {
		return fmt.Errorf("ssh resolves HostKeyAlias for %s to %q, expected %q", alias, cfg["hostkeyalias"], alias)
	}
	return nil
}

// ---- address helpers ----

// isIPv4Literal reports whether s is a canonical dotted-quad IPv4 address.
func isIPv4Literal(s string) bool {
	a, err := netip.ParseAddr(s)
	return err == nil && a.Is4() && a.String() == s
}

package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	auditMaxCommand  = 4096
	auditDefaultRead = 200
	auditMaxRead     = 1000
	auditKeepFiles   = 5 // rotated files exec.jsonl.1 .. exec.jsonl.5
)

// auditMaxBytes is the size at which exec.jsonl is rotated. A variable so tests can shrink it.
var auditMaxBytes int64 = 10 << 20

// auditEntry is one line of <data dir>/audit/exec.jsonl.
//
// The audit log is an OPERATIONAL log: it answers "who ran or changed what, and when" for the
// operator. It is NOT tamper-proof. It lives in a directory the agent user (and every process the
// agent starts, including commands run through /api/exec) can write, it has no hash chain and no
// off-host copy besides the daily backup, and a restore of the data directory rewinds it. Do not
// treat it as evidence against a privileged actor. Rotation (auditMaxBytes, auditKeepFiles) only
// bounds its size.
type auditEntry struct {
	TS         string         `json:"ts"`
	Actor      string         `json:"actor"`
	Route      string         `json:"route"`
	Targets    []string       `json:"targets"`
	Command    string         `json:"command"`
	ExitCodes  map[string]int `json:"exit_codes"`
	DurationMs int64          `json:"duration_ms"`
	Status     string         `json:"status"` // ok, error, or rejected (request refused before anything ran)
	// Detail is an optional free-text summary (operation, error). Absent on plain exec entries.
	Detail string `json:"detail,omitempty"`
	// Source, Op and Project say which engine feature sent the command (auditOrigin); absent for the dashboard.
	Source  string `json:"source,omitempty"`
	Op      string `json:"op,omitempty"`
	Project string `json:"project,omitempty"`
	// ActionID is the decision action an approved or automatic run belongs to (X-Guardian-Action, digits only).
	ActionID string `json:"action_id,omitempty"`
}

// Header values the debate engine sets on its command requests: X-Guardian-Source ("ops_check" for a read-only
// server check, "action" for an approved action), X-Guardian-Op (the check or tool) and X-Guardian-Project.
// They are labels for the operator, not credentials: any token holder can set them, and withAuth has already
// decided whether the request may run. Values outside auditLabel are dropped, so a header cannot forge a line.
var auditSources = map[string]bool{"ops_check": true, "action": true}

func auditLabel(v string) string {
	v = strings.TrimSpace(v)
	if v == "" || len(v) > 64 {
		return ""
	}
	for _, c := range v {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("_.:-", c)) {
			return ""
		}
	}
	return v
}

// auditOrigin reads the engine's labels from a request (all empty for the dashboard or an unknown source).
func auditOrigin(r *http.Request) (source, op, project, action string) {
	source = auditLabel(r.Header.Get("X-Guardian-Source"))
	if !auditSources[source] {
		return "", "", "", ""
	}
	action = strings.TrimSpace(r.Header.Get("X-Guardian-Action"))
	if len(action) > 12 || strings.Trim(action, "0123456789") != "" {
		action = ""
	}
	return source, auditLabel(r.Header.Get("X-Guardian-Op")), auditLabel(r.Header.Get("X-Guardian-Project")), action
}

var auditMu sync.Mutex

// guardianDataDir is GUARDIAN_DATA_DIR or ~/.guardian/data, as the debate handlers resolve it.
func guardianDataDir() string {
	if d := os.Getenv("GUARDIAN_DATA_DIR"); d != "" {
		return d
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".guardian", "data")
}

func auditFilePath() string { return filepath.Join(guardianDataDir(), "audit", "exec.jsonl") }

func truncateRunes(s string, n int) string {
	if len(s) <= n {
		return s
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// auditActor names who made the request: "session@<ip>" for the dashboard cookie, "token@<ip>"
// for a Bearer AGENT_TOKEN, and the legacy "dashboard@<ip>" when neither is present (the
// no-auth loopback mode). withAuth has already validated the credential before a handler runs.
func auditActor(r *http.Request) string {
	kind := ""
	if strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") {
		kind = "token"
	} else if _, err := r.Cookie(sessionCookie); err == nil {
		kind = "session"
	}
	base := actorFor(r.RemoteAddr)
	if kind == "" {
		return base
	}
	return kind + strings.TrimPrefix(base, "dashboard")
}

// recordExec appends one audit line. A failure is logged and never reaches the caller.
// status is "error" when nothing ran (no exit codes) or any exit code is non-zero.
func recordExec(r *http.Request, route string, targets []string, command string, codes map[string]int, start time.Time) {
	status := "ok"
	if len(codes) == 0 {
		status = "error"
	}
	for _, c := range codes {
		if c != 0 {
			status = "error"
		}
	}
	recordAudit(r, route, targets, command, status, "", codes, start)
}

// recordRejected logs a command request refused before anything ran (validation 400, unknown
// target, hub guard). detail holds the reason.
func recordRejected(r *http.Request, route string, targets []string, command, detail string, start time.Time) {
	recordAudit(r, route, targets, command, "rejected", detail, nil, start)
}

func recordAudit(r *http.Request, route string, targets []string, command, status, detail string, codes map[string]int, start time.Time) {
	if targets == nil {
		targets = []string{}
	}
	if codes == nil {
		codes = map[string]int{}
	}
	e := auditEntry{
		TS:         time.Now().UTC().Format(time.RFC3339),
		Actor:      auditActor(r),
		Route:      route,
		Targets:    targets,
		Command:    truncateRunes(command, auditMaxCommand),
		ExitCodes:  codes,
		DurationMs: time.Since(start).Milliseconds(),
		Status:     status,
		Detail:     truncateRunes(detail, auditMaxCommand),
	}
	e.Source, e.Op, e.Project, e.ActionID = auditOrigin(r)
	if err := appendAudit(e); err != nil {
		log.Printf("audit: write failed: %v", err)
	}
}

// rotateAuditLocked shifts exec.jsonl -> .1 -> ... -> .N (the oldest is dropped). auditMu is held.
func rotateAuditLocked(p string) {
	_ = os.Remove(p + "." + strconv.Itoa(auditKeepFiles))
	for i := auditKeepFiles - 1; i >= 1; i-- {
		_ = os.Rename(p+"."+strconv.Itoa(i), p+"."+strconv.Itoa(i+1))
	}
	if err := os.Rename(p, p+".1"); err != nil {
		log.Printf("audit: rotate failed: %v", err)
	}
}

func appendAudit(e auditEntry) error {
	line, err := json.Marshal(e)
	if err != nil {
		return err
	}
	line = append(line, '\n')
	auditMu.Lock()
	defer auditMu.Unlock()
	p := auditFilePath()
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	if st, serr := os.Stat(p); serr == nil && st.Size() > 0 && st.Size()+int64(len(line)) > auditMaxBytes {
		rotateAuditLocked(p)
	}
	f, err := os.OpenFile(p, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	_, werr := f.Write(line)
	if cerr := f.Close(); werr == nil {
		werr = cerr
	}
	return werr
}

// readAuditTail returns up to limit entries, newest first, across the current file and the
// rotated files exec.jsonl.1 .. .N (newer to older) until limit is reached.
func readAuditTail(limit int) ([]auditEntry, error) {
	auditMu.Lock()
	defer auditMu.Unlock()
	base := auditFilePath()
	out := []auditEntry{}
	for i := 0; i <= auditKeepFiles && len(out) < limit; i++ {
		p := base
		if i > 0 {
			p = base + "." + strconv.Itoa(i)
		}
		part, err := readFileTail(p, limit-len(out))
		if err != nil {
			return nil, err
		}
		out = append(out, part...)
	}
	return out, nil
}

// readFileTail returns up to limit entries of one file, newest first, reading backwards from its end.
func readFileTail(path string, limit int) ([]auditEntry, error) {
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return []auditEntry{}, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	const chunk = 64 * 1024
	pos := st.Size()
	var buf []byte
	for pos > 0 && bytes.Count(buf, []byte("\n")) <= limit {
		n := int64(chunk)
		if pos < n {
			n = pos
		}
		pos -= n
		part := make([]byte, n)
		if _, err := f.ReadAt(part, pos); err != nil && err != io.EOF {
			return nil, err
		}
		buf = append(part, buf...)
	}
	lines := bytes.Split(buf, []byte("\n"))
	if pos > 0 && len(lines) > 0 {
		lines = lines[1:] // the first line may be cut by the chunk boundary
	}
	out := []auditEntry{}
	for i := len(lines) - 1; i >= 0 && len(out) < limit; i-- {
		if len(bytes.TrimSpace(lines[i])) == 0 {
			continue
		}
		var e auditEntry
		if json.Unmarshal(lines[i], &e) == nil {
			out = append(out, e)
		}
	}
	return out, nil
}

func handleAudit(w http.ResponseWriter, r *http.Request) {
	limit := auditDefaultRead
	if v := r.URL.Query().Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			jsonResponse(w, http.StatusBadRequest, map[string]string{"error": "limit must be a positive integer"})
			return
		}
		limit = n
	}
	if limit > auditMaxRead {
		limit = auditMaxRead
	}
	entries, err := readAuditTail(limit)
	if err != nil {
		jsonResponse(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	jsonResponse(w, http.StatusOK, map[string]any{"entries": entries})
}

func sortedKeys(m map[string]int) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// auditOp collects the outcome of one dashboard write (node register/update/delete, revoke-key)
// and records it when done() runs. The status starts as "error"; ok() flips it.
type auditOp struct {
	r       *http.Request
	route   string
	targets []string
	start   time.Time
	status  string
	detail  string
}

func beginAudit(r *http.Request, route string, targets ...string) *auditOp {
	var t []string
	for _, x := range targets {
		if x != "" {
			t = append(t, x)
		}
	}
	return &auditOp{r: r, route: route, targets: t, start: time.Now(), status: "error"}
}

func (a *auditOp) fail(err error) { a.detail = err.Error() }

func (a *auditOp) ok(detail string) {
	a.status = "ok"
	a.detail = detail
}

func (a *auditOp) done() {
	recordAudit(a.r, a.route, a.targets, "", a.status, a.detail, nil, a.start)
}

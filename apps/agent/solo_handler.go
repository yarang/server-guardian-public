package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

// Solo agent API (docs/design/solo-agent.md, phase S2). One role of a project answers one question alone:
// no Judge, no round, no thread, read-only. The run is a guardian.py --agent-run child that stores its own
// record under <data dir>/solo-runs/<id>/ (result.json, events.jsonl); this file only validates the request,
// spawns the child without a shell, and reads the records back. A solo run is not a debate job: it never
// appears in /api/debates and has its own slot count (GUARDIAN_MAX_SOLO).

const (
	defaultMaxSolo        = 2
	defaultSoloStartGrace = 3 * time.Second
	soloBodyLimit         = 16 << 10
	// soloResultLimit is the largest result.json the API reads back; a run stores far less.
	soloResultLimit      = 4 << 20
	soloLanguageChars    = 40
	soloListQuestionChar = 200
	soloLabel            = "SOLO"
	soloNote             = "unverified, no Judge"
	soloGeneralNote      = "unverified, no Judge, no sources"
	// soloTimeoutLine is appended to the run's log when the job timeout kills it, so GET shows why.
	soloTimeoutLine = `{"ok": false, "error": "timed out after %s"}`
)

var (
	soloIDPattern       = regexp.MustCompile(`^solo_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$`)
	soloLanguagePattern = regexp.MustCompile(`^[\p{L}][\p{L} ()-]{0,39}$`)
)

func (c debateConfig) soloStartGrace() time.Duration {
	if c.SoloStartGrace > 0 {
		return c.SoloStartGrace
	}
	return defaultSoloStartGrace
}

func (c debateConfig) soloSlots() int {
	if c.MaxSolo > 0 {
		return c.MaxSolo
	}
	return defaultMaxSolo
}

func (h *debateHub) soloDir() string { return filepath.Join(h.cfg.DataDir, "solo-runs") }

func (h *debateHub) registerSoloRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/solo", h.guard(h.startSolo))
	mux.HandleFunc("GET /api/solo", h.guard(h.listSolo))
	mux.HandleFunc("GET /api/solo/{id}", h.guard(h.getSolo))
}

func newSoloID() string {
	var b [3]byte
	_, _ = rand.Read(b[:])
	return "solo_" + time.Now().UTC().Format("20060102_150405") + "_" + hex.EncodeToString(b[:])
}

type soloRequest struct {
	Question string `json:"question"`
	// Project defaults to the CLI's default project when empty (unless General is set).
	Project string `json:"project"`
	// General asks a question without a project: no project code or server checks, answered from
	// general knowledge by any provider but zai (the CLI enforces that) and labelled "no sources". It excludes Project and the context fields.
	General bool `json:"general"`
	// Role is a persona id (default: the default team's expert); the CLI refuses roles that judge.
	Role     string `json:"role"`
	Provider string `json:"provider"`
	// Language names the answer language (default Korean, set by the CLI).
	Language     string   `json:"language"`
	ContextPaths []string `json:"context_paths"`
	ContextRef   string   `json:"context_ref"`
}

// validate bounds the shape of every field; the CLI is the only validator of what they mean.
func (req *soloRequest) validate() string {
	switch {
	case strings.TrimSpace(req.Question) == "":
		return "question required"
	case utf8.RuneCountInString(req.Question) > maxTaskChars:
		return fmt.Sprintf("question must be at most %d characters", maxTaskChars)
	case strings.ContainsRune(req.Question, 0):
		return "question contains invalid characters"
	case req.Role != "" && !idPattern.MatchString(req.Role):
		return "invalid role"
	case req.Provider != "" && !idPattern.MatchString(req.Provider):
		return "invalid provider"
	case req.Language != "" && !soloLanguagePattern.MatchString(req.Language):
		return "invalid language"
	}
	if req.General && (req.Project != "" || len(req.ContextPaths) > 0 || req.ContextRef != "") {
		return "a general question takes no project, context paths or ref"
	}
	if req.General {
		return ""
	}
	// Project, context paths and ref follow the debate rules.
	shape := debateRequest{Task: "x", Project: req.Project, ContextPaths: req.ContextPaths, ContextRef: req.ContextRef}
	return shape.validate()
}

func (req *soloRequest) args(id string) []string {
	// Single "=" entries, so a value that starts with "-" can never be read as an option.
	args := []string{"--agent-run=" + req.Question, "--solo-id=" + id, "--json"}
	if req.General {
		args = append(args, "--general")
	}
	if req.Project != "" {
		args = append(args, "--project="+req.Project)
	}
	if req.Role != "" {
		args = append(args, "--role="+req.Role)
	}
	if req.Provider != "" {
		args = append(args, "--provider="+req.Provider)
	}
	if req.Language != "" {
		args = append(args, "--language="+req.Language)
	}
	for _, p := range req.ContextPaths {
		args = append(args, "--context-path="+p)
	}
	if req.ContextRef != "" {
		args = append(args, "--context-ref="+req.ContextRef)
	}
	return args
}

// soloProc is a started solo child; done closes after it has exited and its log is complete.
type soloProc struct {
	done     chan struct{}
	exit     int
	timedOut bool
}

// @MX:WARN: [AUTO] Starts a guardian.py --agent-run child, which runs an LLM CLI on the hub with read-only repository or server-check tools.
// @MX:REASON: Request fields reach the child only as validated "=" argv entries (never a shell); at most GUARDIAN_MAX_SOLO runs at once (counted here and by the CLI's flock slots); the job timeout kills the whole process group; the child cannot propose or run actions.
//
// startSolo starts a solo run: {"question", "project"? or "general"?, "role"?, "provider"?, "language"?, "context_paths"?,
// "context_ref"?}. The id is made here and passed as --solo-id, so the answer is 202 {"id", "status"} at once.
// The CLI refuses invalid input or full slots with exit 2 or 4 right after it starts; the route waits
// SoloStartGrace for that and answers 400 or 429 (the record is then removed).
func (h *debateHub) startSolo(w http.ResponseWriter, r *http.Request) {
	var req soloRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, soloBodyLimit))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		var mbe *http.MaxBytesError
		switch {
		case errors.As(err, &mbe):
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
		default:
			errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		}
		return
	}
	if msg := req.validate(); msg != "" {
		errJSON(w, http.StatusBadRequest, msg)
		return
	}
	id := newSoloID()
	slots := h.cfg.soloSlots()
	h.mu.Lock()
	if len(h.soloRuns) >= slots {
		h.mu.Unlock()
		errJSON(w, http.StatusTooManyRequests, "too many running solo runs (max "+strconv.Itoa(slots)+")")
		return
	}
	h.soloRuns[id] = struct{}{}
	h.mu.Unlock()

	proc, err := h.launchSolo(id, req.args(id))
	if err != nil {
		h.mu.Lock()
		delete(h.soloRuns, id)
		h.mu.Unlock()
		_ = os.RemoveAll(filepath.Join(h.soloDir(), id))
		errJSON(w, http.StatusInternalServerError, "cannot start solo run: "+err.Error())
		return
	}
	select {
	case <-proc.done:
		h.mu.Lock()
		rejected := !proc.timedOut && (proc.exit == 2 || proc.exit == 4)
		h.mu.Unlock()
		if rejected {
			dir := filepath.Join(h.soloDir(), id)
			found, text := logErrorLine(filepath.Join(dir, "output.log"))
			if found == "" {
				found = joinErr("solo run exited with code "+strconv.Itoa(proc.exit), tailString(text, errLogTailBytes))
			}
			_ = os.RemoveAll(dir)
			status := http.StatusBadRequest
			if proc.exit == 4 {
				status = http.StatusTooManyRequests
			}
			errJSON(w, status, found)
			return
		}
	case <-time.After(h.cfg.soloStartGrace()):
	}
	view, _ := h.readSolo(id)
	note := soloNote
	if req.General {
		note = soloGeneralNote
	}
	jsonResponse(w, http.StatusAccepted, map[string]interface{}{
		"id": id, "status": view.Status, "label": soloLabel, "note": note,
	})
}

// launchSolo creates the record directory, starts the child and the goroutine that waits for it. The
// caller has put id into h.soloRuns, which the goroutine removes once the child has exited.
func (h *debateHub) launchSolo(id string, args []string) (*soloProc, error) {
	return h.launchRecordRun(filepath.Join(h.soloDir(), id), args, func() { delete(h.soloRuns, id) })
}

// launchRecordRun starts a CLI child that writes its own record into dir (solo runs, quick reviews): it
// creates dir and output.log, starts the child in its own process group, kills the group at the job
// timeout and once it has exited, and calls release (with h.mu held) before closing done.
func (h *debateHub) launchRecordRun(dir string, args []string, release func()) (*soloProc, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	logFile, err := os.OpenFile(filepath.Join(dir, "output.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	cmd := h.newCmd(context.Background(), args...)
	cmd.Stdout, cmd.Stderr = logFile, logFile
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return nil, err
	}
	pid := cmd.Process.Pid
	kill := func() { _ = syscall.Kill(-pid, syscall.SIGKILL) }
	proc := &soloProc{done: make(chan struct{})}
	timer := time.AfterFunc(h.cfg.JobTimeout, func() {
		h.mu.Lock()
		proc.timedOut = true
		h.mu.Unlock()
		kill()
	})
	go func() {
		waitErr := cmd.Wait()
		timer.Stop()
		kill() // reap any grandchildren left behind
		exit := 0
		if waitErr != nil {
			exit = -1
			var ee *exec.ExitError
			if errors.As(waitErr, &ee) {
				exit = ee.ExitCode()
			}
		}
		h.mu.Lock()
		timedOut := proc.timedOut
		h.mu.Unlock()
		if timedOut {
			_, _ = fmt.Fprintf(logFile, "\n"+soloTimeoutLine+"\n", h.cfg.JobTimeout)
		}
		logFile.Close()
		h.mu.Lock()
		proc.exit = exit
		release()
		h.mu.Unlock()
		close(proc.done)
	}()
	return proc, nil
}

// soloView is one run as GET /api/solo/{id} shows it.
type soloView struct {
	ID     string `json:"id"`
	Status string `json:"status"` // running, completed or failed
	Label  string `json:"label"`
	Note   string `json:"note"`
	Error  string `json:"error,omitempty"`
	// Result is the stored result.json, passed through unchanged.
	Result json.RawMessage `json:"result,omitempty"`
}

// readSolo describes the record of id; ok is false when there is none. Only a real directory directly
// under solo-runs is read (a symlink is refused), and only result.json and output.log inside it.
func (h *debateHub) readSolo(id string) (soloView, bool) {
	view := soloView{ID: id, Label: soloLabel, Note: soloNote}
	dir := filepath.Join(h.soloDir(), id)
	if fi, err := os.Lstat(dir); err != nil || !fi.IsDir() {
		return view, false
	}
	if raw, ok := readRegularFile(filepath.Join(dir, "result.json"), soloResultLimit); ok && json.Valid(raw) {
		view.Status, view.Result = "completed", raw
		var stored struct {
			Note string `json:"note"`
		}
		if json.Unmarshal(raw, &stored) == nil && stored.Note != "" {
			view.Note = stored.Note // the run knows its own label text (a general answer says "no sources")
		}
		return view, true
	}
	h.mu.Lock()
	_, running := h.soloRuns[id]
	h.mu.Unlock()
	if running {
		view.Status = "running"
		return view, true
	}
	view.Status = "failed"
	found, text := logErrorLine(filepath.Join(dir, "output.log"))
	switch {
	case found != "":
		view.Error = found
	case text != "":
		view.Error = tailString(text, errLogTailBytes)
	default:
		view.Error = "the run ended without a result (interrupted or failed)"
	}
	return view, true
}

// readRegularFile reads a regular file of at most limit bytes, never through a symlink.
func readRegularFile(path string, limit int64) ([]byte, bool) {
	fi, err := os.Lstat(path)
	if err != nil || !fi.Mode().IsRegular() || fi.Size() > limit {
		return nil, false
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(data)) > limit {
		return nil, false
	}
	return data, true
}

func (h *debateHub) getSolo(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !soloIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	view, ok := h.readSolo(id)
	if !ok {
		errJSON(w, http.StatusNotFound, "solo run not found")
		return
	}
	jsonResponse(w, http.StatusOK, view)
}

// soloListItem is one row of GET /api/solo: the state and, once finished, the facts worth a list row.
type soloListItem struct {
	ID       string  `json:"id"`
	Status   string  `json:"status"`
	Label    string  `json:"label"`
	Note     string  `json:"note"`
	Error    string  `json:"error,omitempty"`
	Project  string  `json:"project,omitempty"`
	Mode     string  `json:"mode,omitempty"` // code, ops or general (once the run has a result)
	Role     string  `json:"role,omitempty"`
	Provider string  `json:"provider,omitempty"`
	Question string  `json:"question,omitempty"`
	Summary  string  `json:"summary,omitempty"`
	Degraded bool    `json:"degraded,omitempty"`
	Created  string  `json:"created,omitempty"`
	Elapsed  float64 `json:"elapsed_s,omitempty"`
}

func (h *debateHub) listSolo(w http.ResponseWriter, _ *http.Request) {
	entries, _ := os.ReadDir(h.soloDir())
	ids := make([]string, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() && e.Type()&os.ModeSymlink == 0 && soloIDPattern.MatchString(e.Name()) {
			ids = append(ids, e.Name())
		}
	}
	// The id starts with its start time, so the newest sorts last.
	sort.Sort(sort.Reverse(sort.StringSlice(ids)))
	if len(ids) > listLimit {
		ids = ids[:listLimit]
	}
	runs := make([]soloListItem, 0, len(ids))
	for _, id := range ids {
		view, ok := h.readSolo(id)
		if !ok {
			continue
		}
		item := soloListItem{ID: id, Status: view.Status, Label: soloLabel, Note: view.Note, Error: view.Error}
		if view.Result != nil {
			var res struct {
				Project  string  `json:"project"`
				Mode     string  `json:"mode"`
				Role     string  `json:"role"`
				Provider string  `json:"provider"`
				Question string  `json:"question"`
				Summary  string  `json:"summary"`
				Degraded bool    `json:"degraded"`
				Created  string  `json:"created"`
				Elapsed  float64 `json:"elapsed_s"`
			}
			if json.Unmarshal(view.Result, &res) == nil {
				item.Project, item.Mode, item.Role, item.Provider = res.Project, res.Mode, res.Role, res.Provider
				item.Question, item.Summary, item.Degraded = clipRunes(res.Question, soloListQuestionChar), res.Summary, res.Degraded
				item.Created, item.Elapsed = res.Created, res.Elapsed
			}
		}
		runs = append(runs, item)
	}
	jsonResponse(w, http.StatusOK, map[string]interface{}{"runs": runs})
}

// clipRunes shortens s to at most n runes, marking the cut.
func clipRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n]) + "…"
}

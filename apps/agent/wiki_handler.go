package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Code wiki API (docs/design/debate-code-index-wiki.md, "Go API와 대시보드" and "구현 메모 (P4)").
// Pages are read, saved and deleted through the guardian.py wiki commands, which validate everything
// and own the files. A build is a background job in the debate jobs directory (meta kind "code_wiki")
// with its own concurrency slot, so the Agents monitor and GET /api/debates/{id}/events follow it like
// a debate and DELETE /api/debates/{id} cancels it.

const (
	kindCodeWiki = "code_wiki"
	// wikiBodyLimit is the largest PUT request: wikiTextLimit bytes of page text plus JSON escaping and
	// the title and scope; it matches the CLI's own input cap.
	wikiBodyLimit = 320 << 10
	// wikiTextLimit is the largest page text (UTF-8 bytes), as in code_wiki.py.
	wikiTextLimit      = 256 << 10
	wikiBuildBodyLimit = 8 << 10
	maxWikiBuildPages  = 50
	// wikiCLITimeout bounds the read/save/delete commands; listing computes every page's state against
	// the code index, which can take a while on a first run.
	wikiCLITimeout        = 120 * time.Second
	defaultWikiStartGrace = 3 * time.Second
)

var wikiPagePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)

func (h *debateHub) registerWikiRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/projects/{id}/wiki", h.guard(h.wikiList))
	mux.HandleFunc("POST /api/projects/{id}/wiki/build", h.guard(h.wikiBuild))
	mux.HandleFunc("GET /api/projects/{id}/wiki/{page}", h.guard(h.wikiShow))
	mux.HandleFunc("PUT /api/projects/{id}/wiki/{page}", h.guard(h.wikiSave))
	mux.HandleFunc("DELETE /api/projects/{id}/wiki/{page}", h.guard(h.wikiDelete))
	h.registerFileRoutes(mux)
}

// wikiParams validates the {id} project and optional ?sha= values, writing 400 on failure.
func wikiParams(w http.ResponseWriter, r *http.Request, wantSHA bool) (project, sha string, ok bool) {
	project = r.PathValue("id")
	if !idPattern.MatchString(project) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return "", "", false
	}
	if wantSHA {
		sha = r.URL.Query().Get("sha")
		if sha != "" && (strings.HasPrefix(sha, "-") || !contextRefRe.MatchString(sha)) {
			errJSON(w, http.StatusBadRequest, "invalid sha")
			return "", "", false
		}
	}
	return project, sha, true
}

func wikiPageParam(w http.ResponseWriter, r *http.Request) (string, bool) {
	page := r.PathValue("page")
	if !wikiPagePattern.MatchString(page) {
		errJSON(w, http.StatusBadRequest, "invalid page id")
		return "", false
	}
	return page, true
}

func (h *debateHub) wikiList(w http.ResponseWriter, r *http.Request) {
	project, sha, ok := wikiParams(w, r, true)
	if !ok {
		return
	}
	args := []string{"--code-wiki-list", "--project=" + project, "--json"}
	if sha != "" {
		args = append(args, "--sha="+sha)
	}
	code, out, errOut, err := h.runCLI(wikiCLITimeout, nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

func (h *debateHub) wikiShow(w http.ResponseWriter, r *http.Request) {
	project, sha, ok := wikiParams(w, r, true)
	if !ok {
		return
	}
	page, ok := wikiPageParam(w, r)
	if !ok {
		return
	}
	args := []string{"--code-wiki-show=" + page, "--project=" + project, "--json"}
	if sha != "" {
		args = append(args, "--sha="+sha)
	}
	code, out, errOut, err := h.runCLI(wikiCLITimeout, nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

// wikiSave stores a person's edit. The body is {title, scope, body[, id]}; the {page} path value
// is the page id and must equal a body id when one is given. The CLI validates the rest and rewrites
// the front matter (author = the actor, basis from the citations).
func (h *debateHub) wikiSave(w http.ResponseWriter, r *http.Request) {
	project, _, ok := wikiParams(w, r, false)
	if !ok {
		return
	}
	page, ok := wikiPageParam(w, r)
	if !ok {
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, wikiBodyLimit))
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "cannot read request body")
		return
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil || fields == nil {
		errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	if rawID, has := fields["id"]; has {
		var bodyID string
		if json.Unmarshal(rawID, &bodyID) != nil || bodyID != page {
			errJSON(w, http.StatusBadRequest, "body id must equal the page id in the path")
			return
		}
	}
	pageJSON, _ := json.Marshal(page)
	fields["id"] = pageJSON
	if rawBody, has := fields["body"]; has {
		var text string
		if json.Unmarshal(rawBody, &text) == nil && len(text) > wikiTextLimit {
			errJSON(w, http.StatusRequestEntityTooLarge, "page text too large")
			return
		}
	}
	stdin, _ := json.Marshal(fields)
	code, out, errOut, err := h.runCLI(wikiCLITimeout, stdin,
		"--code-wiki-save", "--project="+project, "--actor="+actorFor(r.RemoteAddr), "--json")
	h.respondConfig(w, code, out, errOut, err)
}

func (h *debateHub) wikiDelete(w http.ResponseWriter, r *http.Request) {
	project, _, ok := wikiParams(w, r, false)
	if !ok {
		return
	}
	page, ok := wikiPageParam(w, r)
	if !ok {
		return
	}
	code, out, errOut, err := h.runCLI(wikiCLITimeout, nil, "--code-wiki-delete="+page, "--project="+project, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

type wikiBuildRequest struct {
	Pages []string `json:"pages"`
	Stale bool     `json:"stale"`
	All   bool     `json:"all"`
	Force bool     `json:"force"`
	// Kind (dir, overview, group or feature) limits stale/all to that kind; alone it builds every candidate
	// page of it (group: the group pages of large directories, then their overview pages).
	Kind string `json:"kind"`
}

// args returns the job's task text and the validated selection flags. No selection builds the default
// page set, which the CLI only allows for a project without pages (it answers exit 2 otherwise).
func (req *wikiBuildRequest) args() (task string, flags []string, msg string) {
	if len(req.Pages) > maxWikiBuildPages {
		return "", nil, "pages must have at most " + strconv.Itoa(maxWikiBuildPages) + " entries"
	}
	selected := 0
	if len(req.Pages) > 0 {
		selected++
	}
	if req.Stale {
		selected++
	}
	if req.All {
		selected++
	}
	if selected > 1 {
		return "", nil, "choose one of pages, stale or all"
	}
	switch req.Kind {
	case "", "dir", "overview", "group", "feature":
	default:
		return "", nil, "kind must be dir, overview, group or feature"
	}
	if req.Kind != "" && len(req.Pages) > 0 {
		return "", nil, "kind selects pages itself: do not combine it with pages"
	}
	switch {
	case len(req.Pages) > 0:
		for _, p := range req.Pages {
			if !wikiPagePattern.MatchString(p) {
				return "", nil, "invalid page id"
			}
			flags = append(flags, "--page="+p)
		}
		task = "code wiki build: pages " + strings.Join(req.Pages, ", ")
	case req.Stale:
		flags, task = []string{"--stale"}, "code wiki build: stale pages"
	case req.All:
		flags, task = []string{"--all"}, "code wiki build: all pages"
	case req.Kind != "":
		task = "code wiki build: " + req.Kind + " pages"
	default:
		task = "code wiki build: default page set"
	}
	if req.Kind != "" {
		flags = append(flags, "--kind="+req.Kind)
		if req.Stale || req.All {
			task += " (" + req.Kind + ")"
		}
	}
	if req.Force {
		flags = append(flags, "--force")
		task += " (force)"
	}
	return task, flags, ""
}

// @MX:WARN: [AUTO] Starts a background guardian.py code wiki build, which runs LLM sessions on the hub for every page.
// @MX:REASON: One build slot (GUARDIAN_MAX_WIKI_BUILDS, default 1) apart from the debate limit, one build per project (the CLI's flock, exit 4 -> 409), the job timeout and DELETE /api/debates/{id} bound it; inputs reach the child only as validated argv entries.
//
// wikiBuild starts a code wiki build job: {"pages": [ids]} | {"stale": true} | {"all": true}, optional
// "force"; with no selection the default page set (only for a project without pages). The answer is
// 202 with the job summary (kind "code_wiki"). The CLI refuses invalid input, an unknown page or a
// running build of the same project with exit 2, 3 or 4 right after it starts; the route waits
// WikiStartGrace for that and answers 400, 404 or 409 (the job directory is then removed).
func (h *debateHub) wikiBuild(w http.ResponseWriter, r *http.Request) {
	project, _, ok := wikiParams(w, r, false)
	if !ok {
		return
	}
	var req wikiBuildRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, wikiBuildBodyLimit))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil && !errors.Is(err, io.EOF) {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	task, flags, msg := req.args()
	if msg != "" {
		errJSON(w, http.StatusBadRequest, msg)
		return
	}
	args := append([]string{"--code-wiki-build", "--project=" + project, "--json"}, flags...)

	run := &debateRun{done: make(chan struct{}), summary: debateSummary{
		ID: newJobID(), Task: task, Project: project, Kind: kindCodeWiki,
		Status: "running", StartedAt: time.Now().UTC(),
	}}
	slots := h.cfg.MaxWikiBuilds
	if slots < 1 {
		slots = 1
	}
	h.mu.Lock()
	running := 0
	for _, other := range h.runs {
		if other.summary.Kind != kindCodeWiki || other.summary.Status != "running" {
			continue
		}
		if other.summary.Project == project {
			h.mu.Unlock()
			errJSON(w, http.StatusConflict, "a code wiki build is already running for this project; wait for it to finish or cancel it")
			return
		}
		running++
	}
	if running >= slots {
		h.mu.Unlock()
		errJSON(w, http.StatusTooManyRequests, "too many running code wiki builds (max "+strconv.Itoa(slots)+")")
		return
	}
	h.runs[run.summary.ID] = run
	h.mu.Unlock()

	if err := h.launch(run, args); err != nil {
		h.mu.Lock()
		delete(h.runs, run.summary.ID)
		h.mu.Unlock()
		errJSON(w, http.StatusInternalServerError, "cannot start code wiki build: "+err.Error())
		return
	}
	grace := h.cfg.WikiStartGrace
	if grace <= 0 {
		grace = defaultWikiStartGrace
	}
	select {
	case <-run.done:
		h.mu.Lock()
		s := run.summary
		rejected := !run.cancelled && !run.timedOut && s.ExitCode != nil &&
			(*s.ExitCode == 2 || *s.ExitCode == 3 || *s.ExitCode == 4)
		h.mu.Unlock()
		if rejected {
			dir := filepath.Join(h.jobsDir(), s.ID)
			status := map[int]int{2: http.StatusBadRequest, 3: http.StatusNotFound, 4: http.StatusConflict}[*s.ExitCode]
			msg := wikiBuildError(filepath.Join(dir, "output.log"), *s.ExitCode)
			_ = os.RemoveAll(dir)
			errJSON(w, status, msg)
			return
		}
	case <-time.After(grace):
	}
	h.mu.Lock()
	snapshot := run.summary
	h.mu.Unlock()
	jsonResponse(w, http.StatusAccepted, snapshot)
}

// wikiBuildError extracts the CLI's {"ok": false, "error": ...} line from the job log, falling back
// to the log tail.
func wikiBuildError(logPath string, exit int) string {
	text := strings.TrimSpace(readTail(logPath, errLogTailBytes))
	found := ""
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "{") {
			continue
		}
		var out struct {
			Error string `json:"error"`
		}
		if json.Unmarshal([]byte(line), &out) == nil && out.Error != "" {
			found = out.Error
		}
	}
	if found != "" {
		return found
	}
	return joinErr("code wiki build exited with code "+strconv.Itoa(exit), tailString(text, errLogTailBytes))
}

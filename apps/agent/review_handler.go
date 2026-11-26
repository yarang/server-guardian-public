package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Quick review API (docs/design/verification-layer.md, V1b). One read-only reviewer checks one change of a code
// project and reports findings labelled REVIEW: no Judge, no thread, no action. The run is a guardian.py
// --review child that stores its own record under <data dir>/reviews/<id>/ (result.json, events.jsonl); this
// file validates the request, writes a submitted patch next to the record, spawns the child without a shell
// (the launcher of solo_handler.go) and reads the records back. It has its own slot count (GUARDIAN_MAX_REVIEWS).

const (
	defaultMaxReviews = 2
	// reviewPatchLimit matches the CLI's own patch limit (review_agent.MAX_PATCH_BYTES).
	reviewPatchLimit   = 2 << 20
	reviewBodyLimit    = reviewPatchLimit + 16<<10
	reviewResultLimit  = 8 << 20
	reviewRequestFile  = "request.json"
	reviewRequestLimit = 16 << 10
	reviewLabel        = "REVIEW"
	reviewNote         = "advisory, no Judge"
	reviewPatchFile    = "change.diff"
)

var (
	reviewIDPattern = regexp.MustCompile(`^review_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$`)
	// reviewRangePattern is BASE..HEAD of two refs of the contextRefRe shape (no "...", no leading dash).
	reviewRangePattern = regexp.MustCompile(`^([A-Za-z0-9_/~^][A-Za-z0-9._/~^-]{0,99})\.\.([A-Za-z0-9_/~^][A-Za-z0-9._/~^-]{0,99})$`)
	// reviewBranchPattern is the shape of a branch name of the project's origin; the CLI runs
	// git check-ref-format for git's own full rule.
	reviewBranchPattern    = regexp.MustCompile(`^[A-Za-z0-9._/-]{1,100}$`)
	reviewSHAPattern       = regexp.MustCompile(`^[0-9a-f]{40}$`)
	reviewStartedByPattern = regexp.MustCompile(`^session:[A-Za-z0-9._-]{1,64}$`)
)

func (c debateConfig) reviewSlots() int {
	if c.MaxReviews > 0 {
		return c.MaxReviews
	}
	return defaultMaxReviews
}

func (h *debateHub) reviewDir() string { return filepath.Join(h.cfg.DataDir, "reviews") }

func (h *debateHub) registerReviewRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/reviews", h.guard(h.startReview))
	mux.HandleFunc("GET /api/reviews", h.guard(h.listReviews))
	mux.HandleFunc("GET /api/reviews/{id}", h.guard(h.getReview))
}

func newReviewID() string {
	return "review" + strings.TrimPrefix(newSoloID(), "solo")
}

type reviewRequest struct {
	Project string `json:"project"`
	// Range is BASE..HEAD of the project's workspace; Patch is a unified diff applied to PatchBase (default HEAD);
	// Ref is a branch of the project's configured origin remote that the CLI fetches into a private namespace,
	// narrowed by Base and ExpectSHA; Action is the id of a PROPOSED jev_mcp:apply_patch action of the project.
	// Exactly one of Range, Patch, Ref and Action.
	Range     string `json:"range"`
	Patch     string `json:"patch"`
	PatchBase string `json:"patch_base"`
	Ref       string `json:"ref"`
	Base      string `json:"base"`
	ExpectSHA string `json:"expect_sha"`
	Action    int    `json:"action"`
	// StartedBy labels who asked for the review (session:<workstation> or empty); it is stored in request.json
	// only and never reaches the CLI.
	StartedBy string `json:"started_by"`
	// Role is a persona id (default: the default team's first critic); the CLI refuses roles that judge.
	Role     string `json:"role"`
	Provider string `json:"provider"`
	Language string `json:"language"`
}

// validReviewBranch bounds the shape of a branch name (no leading dash, no "..", no leading or trailing slash,
// no trailing dot or .lock); git check-ref-format in the CLI is the full rule.
func validReviewBranch(ref string) bool {
	if strings.HasPrefix(ref, "-") || !reviewBranchPattern.MatchString(ref) {
		return false
	}
	return !strings.Contains(ref, "..") && !strings.HasPrefix(ref, "/") && !strings.HasSuffix(ref, "/") &&
		!strings.HasSuffix(ref, ".") && !strings.HasSuffix(ref, ".lock")
}

// validate bounds the shape of every field; the CLI is the only validator of what they mean.
func (req *reviewRequest) validate() string {
	hasRange, hasPatch := req.Range != "", strings.TrimSpace(req.Patch) != ""
	hasRef, hasAction := req.Ref != "", req.Action != 0
	sources := 0
	for _, has := range []bool{hasRange, hasPatch, hasRef, hasAction} {
		if has {
			sources++
		}
	}
	switch {
	case req.Project == "":
		return "project required"
	case !idPattern.MatchString(req.Project):
		return "invalid project"
	case sources != 1:
		return "give the change as range (BASE..HEAD), patch, ref (branch) or action (id), one of them"
	case hasRange && (strings.Contains(req.Range, "...") || !reviewRangePattern.MatchString(req.Range)):
		return "range must be BASE..HEAD"
	case len(req.Patch) > reviewPatchLimit:
		return "patch is larger than 2 MiB"
	case strings.ContainsRune(req.Patch, 0):
		return "patch contains invalid characters"
	case req.PatchBase != "" && !hasPatch:
		return "patch_base goes with patch"
	case req.PatchBase != "" && (strings.HasPrefix(req.PatchBase, "-") || !contextRefRe.MatchString(req.PatchBase)):
		return "invalid patch_base"
	case req.Action < 0:
		return "invalid action"
	case req.Base != "" && !hasRef:
		return "base goes with ref"
	case req.ExpectSHA != "" && !hasRef:
		return "expect_sha goes with ref"
	case hasRef && !validReviewBranch(req.Ref):
		return "invalid ref"
	case hasRef && req.Base != "" && (strings.HasPrefix(req.Base, "-") || !contextRefRe.MatchString(req.Base)):
		return "invalid base"
	case req.ExpectSHA != "" && !reviewSHAPattern.MatchString(req.ExpectSHA):
		return "expect_sha must be 40 lowercase hex characters"
	case req.StartedBy != "" && !reviewStartedByPattern.MatchString(req.StartedBy):
		return "invalid started_by"
	case req.Role != "" && !idPattern.MatchString(req.Role):
		return "invalid role"
	case req.Provider != "" && !idPattern.MatchString(req.Provider):
		return "invalid provider"
	case req.Language != "" && !soloLanguagePattern.MatchString(req.Language):
		return "invalid language"
	}
	return ""
}

// reviewSourceRecord is the "source" object of request.json: what was asked for, never the patch text.
type reviewSourceRecord struct {
	Kind      string `json:"kind"` // range, patch, ref or action
	Range     string `json:"range,omitempty"`
	Ref       string `json:"ref,omitempty"`
	Base      string `json:"base,omitempty"`
	ExpectSHA string `json:"expect_sha,omitempty"`
	ActionID  int    `json:"action_id,omitempty"`
}

// reviewRequestRecord is request.json, written at start so list rows can say their source while the review runs.
type reviewRequestRecord struct {
	Project   string             `json:"project"`
	Source    reviewSourceRecord `json:"source"`
	StartedBy string             `json:"started_by"`
}

func (req *reviewRequest) record() reviewRequestRecord {
	rec := reviewRequestRecord{Project: req.Project, StartedBy: req.StartedBy}
	switch {
	case req.Range != "":
		rec.Source = reviewSourceRecord{Kind: "range", Range: req.Range}
	case strings.TrimSpace(req.Patch) != "":
		rec.Source = reviewSourceRecord{Kind: "patch"}
	case req.Ref != "":
		rec.Source = reviewSourceRecord{Kind: "ref", Ref: req.Ref, Base: req.Base, ExpectSHA: req.ExpectSHA}
	default:
		rec.Source = reviewSourceRecord{Kind: "action", ActionID: req.Action}
	}
	return rec
}

// args builds the child's argv; patchPath is the stored patch file (empty for a range, ref or action).
func (req *reviewRequest) args(id, patchPath string) []string {
	// Single "=" entries, so a value that starts with "-" can never be read as an option.
	args := []string{"--review", "--review-id=" + id, "--project=" + req.Project, "--json"}
	switch {
	case patchPath != "":
		args = append(args, "--patch="+patchPath)
		if req.PatchBase != "" {
			args = append(args, "--patch-base="+req.PatchBase)
		}
	case req.Range != "":
		args = append(args, "--range="+req.Range)
	case req.Ref != "":
		args = append(args, "--ref="+req.Ref)
		if req.Base != "" {
			args = append(args, "--base="+req.Base)
		}
		if req.ExpectSHA != "" {
			args = append(args, "--expect-sha="+req.ExpectSHA)
		}
	default:
		args = append(args, "--action="+strconv.Itoa(req.Action))
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
	return args
}

// @MX:WARN: [AUTO] Starts a guardian.py --review child, which runs an LLM CLI on the hub with read-only repository tools.
// @MX:REASON: Request fields reach the child only as validated "=" argv entries (never a shell); a patch is written to a 0600 file in the run's own record directory and applied by the CLI only to a temporary index; at most GUARDIAN_MAX_REVIEWS runs at once; the job timeout kills the process group; the child cannot propose or run actions.
//
// startReview starts a quick review: {"project", "range"? or "patch"? + "patch_base"?, "ref"? + "base"? +
// "expect_sha"?, "action"?, "started_by"?, "role"?, "provider"?, "language"?}. The answer is 202 {"id", "status",
// "label", "note"} at once; the CLI refuses invalid input or full slots with exit 2 or 4 right after it starts,
// which the route waits SoloStartGrace for and answers 400 or 429 (the record is then removed).
func (h *debateHub) startReview(w http.ResponseWriter, r *http.Request) {
	var req reviewRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, reviewBodyLimit))
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
	id := newReviewID()
	slots := h.cfg.reviewSlots()
	h.mu.Lock()
	if len(h.reviewRuns) >= slots {
		h.mu.Unlock()
		errJSON(w, http.StatusTooManyRequests, "too many running reviews (max "+strconv.Itoa(slots)+")")
		return
	}
	h.reviewRuns[id] = struct{}{}
	h.mu.Unlock()

	dir := filepath.Join(h.reviewDir(), id)
	fail := func(status int, msg string) {
		h.mu.Lock()
		delete(h.reviewRuns, id)
		h.mu.Unlock()
		_ = os.RemoveAll(dir)
		errJSON(w, status, msg)
	}
	// request.json says what was asked for (never the patch text), so list rows can show it while the review runs.
	if err := os.MkdirAll(dir, 0o700); err != nil {
		fail(http.StatusInternalServerError, "cannot store the request: "+err.Error())
		return
	}
	if raw, err := json.Marshal(req.record()); err != nil {
		fail(http.StatusInternalServerError, "cannot store the request: "+err.Error())
		return
	} else if err := os.WriteFile(filepath.Join(dir, reviewRequestFile), raw, 0o600); err != nil {
		fail(http.StatusInternalServerError, "cannot store the request: "+err.Error())
		return
	}
	patchPath := ""
	if req.Patch != "" {
		patchPath = filepath.Join(dir, reviewPatchFile)
		if err := os.WriteFile(patchPath, []byte(req.Patch), 0o600); err != nil {
			fail(http.StatusInternalServerError, "cannot store the patch: "+err.Error())
			return
		}
	}
	proc, err := h.launchRecordRun(dir, req.args(id, patchPath), func() { delete(h.reviewRuns, id) })
	if err != nil {
		fail(http.StatusInternalServerError, "cannot start review: "+err.Error())
		return
	}
	select {
	case <-proc.done:
		h.mu.Lock()
		rejected := !proc.timedOut && (proc.exit == 2 || proc.exit == 4)
		h.mu.Unlock()
		if rejected {
			found, text := logErrorLine(filepath.Join(dir, "output.log"))
			if found == "" {
				found = joinErr("review exited with code "+strconv.Itoa(proc.exit), tailString(text, errLogTailBytes))
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
	view, _ := h.readReview(id)
	jsonResponse(w, http.StatusAccepted, map[string]interface{}{
		"id": id, "status": view.Status, "label": reviewLabel, "note": reviewNote,
	})
}

// reviewView is one review as GET /api/reviews/{id} shows it.
type reviewView struct {
	ID     string `json:"id"`
	Status string `json:"status"` // running, completed or failed
	Label  string `json:"label"`
	Note   string `json:"note"`
	Error  string `json:"error,omitempty"`
	// Request is the stored request.json, passed through unchanged (present since V2).
	Request json.RawMessage `json:"request,omitempty"`
	// Result is the stored result.json, passed through unchanged.
	Result json.RawMessage `json:"result,omitempty"`
}

// readReview describes the record of id; ok is false when there is none. Only a real directory directly
// under reviews is read (a symlink is refused), and only request.json, result.json and output.log inside it.
func (h *debateHub) readReview(id string) (reviewView, bool) {
	view := reviewView{ID: id, Label: reviewLabel, Note: reviewNote}
	dir := filepath.Join(h.reviewDir(), id)
	if fi, err := os.Lstat(dir); err != nil || !fi.IsDir() {
		return view, false
	}
	if raw, ok := readRegularFile(filepath.Join(dir, reviewRequestFile), reviewRequestLimit); ok && json.Valid(raw) {
		view.Request = raw
	}
	if raw, ok := readRegularFile(filepath.Join(dir, "result.json"), reviewResultLimit); ok && json.Valid(raw) {
		view.Status, view.Result = "completed", raw
		return view, true
	}
	h.mu.Lock()
	_, running := h.reviewRuns[id]
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
		view.Error = "the review ended without a result (interrupted or failed)"
	}
	return view, true
}

func (h *debateHub) getReview(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !reviewIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	view, ok := h.readReview(id)
	if !ok {
		errJSON(w, http.StatusNotFound, "review not found")
		return
	}
	jsonResponse(w, http.StatusOK, view)
}

// reviewCounts are the findings of a finished review by severity.
type reviewCounts struct {
	High   int `json:"high"`
	Medium int `json:"medium"`
	Low    int `json:"low"`
	Notes  int `json:"notes"`
}

// reviewListItem is one row of GET /api/reviews: the state and, once finished, the facts worth a list row.
// SourceKind, Ref, ActionID and StartedBy come from request.json, so they show while a review is still running.
type reviewListItem struct {
	ID         string        `json:"id"`
	Status     string        `json:"status"`
	Label      string        `json:"label"`
	Note       string        `json:"note"`
	Error      string        `json:"error,omitempty"`
	State      string        `json:"state,omitempty"` // done, bad_format or degraded (once there is a result)
	Project    string        `json:"project,omitempty"`
	Range      string        `json:"range,omitempty"`
	Patch      bool          `json:"patch,omitempty"`
	SourceKind string        `json:"source_kind,omitempty"` // range, patch, ref or action
	Ref        string        `json:"ref,omitempty"`
	ActionID   int           `json:"action_id,omitempty"`
	StartedBy  string        `json:"started_by,omitempty"`
	Head       string        `json:"head,omitempty"`
	Role       string        `json:"role,omitempty"`
	Provider   string        `json:"provider,omitempty"`
	Files      int           `json:"files,omitempty"`
	Summary    string        `json:"summary,omitempty"`
	Counts     *reviewCounts `json:"counts,omitempty"`
	Created    string        `json:"created,omitempty"`
	Elapsed    float64       `json:"elapsed_s,omitempty"`
}

// listReviews lists the newest reviews; ?project=ID keeps those of one project, ?action=ID those of one action.
func (h *debateHub) listReviews(w http.ResponseWriter, r *http.Request) {
	project := r.URL.Query().Get("project")
	if project != "" && !idPattern.MatchString(project) {
		errJSON(w, http.StatusBadRequest, "invalid project")
		return
	}
	actionID := 0
	if given := r.URL.Query().Get("action"); given != "" {
		value, err := strconv.Atoi(given)
		if err != nil || value <= 0 {
			errJSON(w, http.StatusBadRequest, "invalid action")
			return
		}
		actionID = value
	}
	entries, _ := os.ReadDir(h.reviewDir())
	ids := make([]string, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() && e.Type()&os.ModeSymlink == 0 && reviewIDPattern.MatchString(e.Name()) {
			ids = append(ids, e.Name())
		}
	}
	// The id starts with its start time, so the newest sorts last.
	sort.Sort(sort.Reverse(sort.StringSlice(ids)))
	runs := make([]reviewListItem, 0, listLimit)
	for _, id := range ids {
		if len(runs) >= listLimit {
			break
		}
		view, ok := h.readReview(id)
		if !ok {
			continue
		}
		item := reviewListItem{ID: id, Status: view.Status, Label: reviewLabel, Note: view.Note, Error: view.Error}
		if view.Request != nil {
			var req reviewRequestRecord
			if json.Unmarshal(view.Request, &req) == nil {
				item.SourceKind, item.Ref, item.ActionID, item.StartedBy = req.Source.Kind, req.Source.Ref,
					req.Source.ActionID, req.StartedBy
				if item.Project == "" {
					item.Project = req.Project // a running review has no result.json to say its project
				}
			}
		}
		if view.Result != nil {
			var res struct {
				State   string `json:"state"`
				Project string `json:"project"`
				Change  struct {
					Range string `json:"range"`
					Patch bool   `json:"patch"`
				} `json:"change"`
				Head     string   `json:"head"`
				Role     string   `json:"role"`
				Provider string   `json:"provider"`
				Files    []string `json:"files"`
				Summary  string   `json:"summary"`
				Findings []struct {
					Severity string `json:"severity"`
				} `json:"findings"`
				Notes   []json.RawMessage `json:"notes"`
				Created string            `json:"created"`
				Elapsed float64           `json:"elapsed_s"`
			}
			if json.Unmarshal(view.Result, &res) == nil {
				counts := &reviewCounts{Notes: len(res.Notes)}
				for _, f := range res.Findings {
					switch f.Severity {
					case "high":
						counts.High++
					case "medium":
						counts.Medium++
					default:
						counts.Low++
					}
				}
				item.State, item.Project, item.Range, item.Patch = res.State, res.Project, res.Change.Range, res.Change.Patch
				item.Head, item.Role, item.Provider, item.Files = res.Head, res.Role, res.Provider, len(res.Files)
				item.Summary, item.Counts, item.Created, item.Elapsed = res.Summary, counts, res.Created, res.Elapsed
			}
		}
		// A running or failed review has no result to say its project; request.json fills it in above.
		if project != "" && item.Project != project {
			continue
		}
		if actionID != 0 && item.ActionID != actionID {
			continue
		}
		runs = append(runs, item)
	}
	jsonResponse(w, http.StatusOK, map[string]interface{}{"runs": runs})
}

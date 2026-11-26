package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"
)

// Hub API for debate teams: persona/team config and debate jobs. The debate engine is the
// Python CLI (guardian.py); this file only validates input, spawns it without a shell and
// tracks jobs on disk. See docs/design/debate-teams-api.md.

const (
	debateUnavailableMsg = "debate engine is not available on this host"
	configBodyLimit      = 64 << 10
	debateBodyLimit      = 16 << 10
	configCLITimeout     = 30 * time.Second
	actionBodyLimit      = 4 << 10
	defaultActionTimeout = 900 * time.Second
	jevRecheckTimeout    = 300 * time.Second
	// planTimeout bounds the read-only plans (publish plan, workspace reset plan).
	planTimeout         = 300 * time.Second
	maxActionID         = 1<<31 - 1
	maxTaskChars        = 4000
	maxDebateRounds     = 10
	logTailBytes        = 64 << 10
	errLogTailBytes     = 2 << 10
	listLimit           = 50
	maxContextPaths     = 20
	maxContextPathChars = 200
)

const (
	// publishTimeout bounds a publish: the project's tests (up to 600s) plus the push (up to 300s).
	publishTimeout   = 1200 * time.Second
	publishBodyLimit = 1 << 10
	// A workspace reset body may name up to maxReleaseThreads threads to release (about 90 bytes each).
	maxReleaseThreads = 20
	resetBodyLimit    = 4 << 10
)

var (
	idPattern     = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)
	threadPattern = regexp.MustCompile(`^thread_[A-Za-z0-9_]{1,80}$`)
	jobIDPattern  = regexp.MustCompile(`^dbt_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$`)
	actionIDForm  = regexp.MustCompile(`^[1-9][0-9]{0,9}$`)
	// mailIDPattern matches resident mail store ids, m-YYYYMMDD-NNNN with a 4-9 digit sequence
	// (mail_store._mail_id; docs/design/resident-monitor.md section 4).
	mailIDPattern       = regexp.MustCompile(`^m-[0-9]{8}-[0-9]{4,9}$`)
	artifactIDPattern   = regexp.MustCompile(`^a-[0-9a-f]{12}$`)
	queryIDPattern      = regexp.MustCompile(`^q-[0-9]{8}-[0-9]{4,9}$`)
	sessionKeyPattern   = regexp.MustCompile(`^(main|project:[A-Za-z0-9][A-Za-z0-9._-]{0,63}(:[a-z0-9][a-z0-9-]{0,23})?)$`)
	residentAddrPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}@[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`)
	actorUnsafe         = regexp.MustCompile(`[^A-Za-z0-9@:._-]`)
	contextRefRe        = regexp.MustCompile(`^[A-Za-z0-9._/~^-]{1,100}$`)
	// branchPattern mirrors the CLI's branch validation (mcp_client.BRANCH_PATTERN); the CLI checks the rest.
	branchPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$`)
)

type debateConfig struct {
	CLI         string
	Python      string
	DataDir     string
	MaxRunning  int
	JobTimeout  time.Duration
	ConfigLimit time.Duration
	// ActionTimeout bounds one judge-action execution (GUARDIAN_ACTION_TIMEOUT seconds).
	ActionTimeout time.Duration
	// UIActions is the raw GUARDIAN_UI_ACTIONS value; "off" (any case) blocks every action/thread decision route (run, reject, fail-stale, jev-recheck, close, publish, workspace-reset) from the dashboard.
	UIActions string
	// MaxWikiBuilds is the slot count of code wiki build jobs (GUARDIAN_MAX_WIKI_BUILDS, default 1). These
	// jobs never count toward MaxRunning (see wiki_handler.go).
	MaxWikiBuilds int
	// WikiStartGrace is how long POST .../wiki/build waits for the CLI to reject the build (exit 2/3/4)
	// before answering 202; 0 means the default.
	WikiStartGrace time.Duration
	// PlanStartGrace is how long POST /api/plans/{id}/run waits for the CLI to refuse the plan (exit 2/3/4)
	// before answering 202; 0 means the default.
	PlanStartGrace time.Duration
	// MaxSolo is the slot count of solo agent runs (GUARDIAN_MAX_SOLO, default 2, like the CLI's own slots).
	MaxSolo int
	// SoloStartGrace is how long POST /api/solo waits for the CLI to refuse the run (exit 2/4) before
	// answering 202; 0 means the default.
	SoloStartGrace time.Duration
	// MaxReviews is the slot count of quick reviews (GUARDIAN_MAX_REVIEWS, default 2, like the CLI's own slots).
	MaxReviews int
}

func (c debateConfig) actionsDisabled() bool {
	return strings.EqualFold(strings.TrimSpace(c.UIActions), "off")
}

func (c debateConfig) actionTimeout() time.Duration {
	if c.ActionTimeout > 0 {
		return c.ActionTimeout
	}
	return defaultActionTimeout
}

func debateConfigFromEnv() debateConfig {
	cfg := debateConfig{
		CLI:         envOr("GUARDIAN_CLI", "cli/llm-agent/guardian.py"),
		Python:      envOr("GUARDIAN_PYTHON", "python3"),
		DataDir:     os.Getenv("GUARDIAN_DATA_DIR"),
		MaxRunning:  2,
		JobTimeout:  time.Hour,
		ConfigLimit: configCLITimeout,

		ActionTimeout: defaultActionTimeout,
		UIActions:     os.Getenv("GUARDIAN_UI_ACTIONS"),

		MaxWikiBuilds:  1,
		WikiStartGrace: defaultWikiStartGrace,
		MaxSolo:        defaultMaxSolo,
		MaxReviews:     defaultMaxReviews,
	}
	if cfg.DataDir == "" {
		home, _ := os.UserHomeDir()
		cfg.DataDir = filepath.Join(home, ".guardian", "data")
	}
	if n, err := strconv.Atoi(os.Getenv("GUARDIAN_MAX_DEBATES")); err == nil && n > 0 {
		cfg.MaxRunning = n
	}
	if n, err := strconv.Atoi(os.Getenv("GUARDIAN_DEBATE_TIMEOUT")); err == nil && n > 0 {
		cfg.JobTimeout = time.Duration(n) * time.Second
	}
	if n, err := strconv.Atoi(os.Getenv("GUARDIAN_ACTION_TIMEOUT")); err == nil && n > 0 {
		cfg.ActionTimeout = time.Duration(n) * time.Second
	}
	if n, err := strconv.Atoi(os.Getenv("GUARDIAN_MAX_WIKI_BUILDS")); err == nil && n > 0 {
		cfg.MaxWikiBuilds = n
	}
	if n, err := strconv.Atoi(os.Getenv("GUARDIAN_MAX_SOLO")); err == nil && n > 0 {
		cfg.MaxSolo = n
	}
	if n, err := strconv.Atoi(os.Getenv("GUARDIAN_MAX_REVIEWS")); err == nil && n > 0 {
		cfg.MaxReviews = n
	}
	return cfg
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// defaultProjectID is the project guardian.py uses when a request names none.
const defaultProjectID = "server-guardian"

// debateSummary is the job summary stored in meta.json and returned by the API.
type debateSummary struct {
	ID         string     `json:"id"`
	Task       string     `json:"task"`
	Project    string     `json:"project,omitempty"`
	Team       string     `json:"team"`
	ThreadID   string     `json:"thread_id"`
	Rounds     int        `json:"rounds"`
	Status     string     `json:"status"`
	StartedAt  time.Time  `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at"`
	ExitCode   *int       `json:"exit_code"`
	Error      string     `json:"error"`
	// ContextPaths and ContextRef echo the code-context request (committed files shown to the agents).
	ContextPaths []string `json:"context_paths,omitempty"`
	ContextRef   string   `json:"context_ref,omitempty"`
	// Template and Params echo a flow-template start (docs/design/flow-templates.md). Task then holds a
	// placeholder until the job finishes and the CLI's result file supplies the rendered task.
	Template string            `json:"template,omitempty"`
	Params   map[string]string `json:"params,omitempty"`
	// Flow and Seats echo a flow start (docs/design/flow-graph.md, G4): the flow id and the characters swapped
	// into its seats. Like a template start, Task holds a placeholder until the result file supplies the task.
	Flow  string            `json:"flow,omitempty"`
	Seats map[string]string `json:"seats,omitempty"`
	// ThreadClosed is set on every job of a thread the user closed (status CLOSED in the decision
	// database) so the dashboard can hide them; a completed debate on the thread clears it.
	ThreadClosed bool `json:"thread_closed,omitempty"`
	// ThreadPublished marks the jobs of a thread with a PUSHED publication. Such a thread is final: it
	// cannot be continued (a new thread must be started). Overlaid from the decision DB when listing.
	ThreadPublished bool `json:"thread_published,omitempty"`
	// Kind is empty for debates and "code_wiki" for code wiki build jobs (wiki_handler.go). Both live in
	// the same jobs directory so the events endpoints and the Agents monitor serve them alike.
	Kind string `json:"kind,omitempty"`
}

// debateListItem is a list entry: the stored summary plus whether an event stream exists.
type debateListItem struct {
	debateSummary
	Events bool `json:"events"`
	// Retrieval is the run's code-retrieval total from result.json; absent for old or running jobs.
	Retrieval *debateRetrieval `json:"retrieval,omitempty"`
}

type debateDetail struct {
	debateSummary
	Log    string          `json:"log"`
	Result json.RawMessage `json:"result"`
	// Retrieval is the same total as in the list entries (absent when result.json has none).
	Retrieval *debateRetrieval `json:"retrieval,omitempty"`
}

// debateRun is the in-memory handle of a running job.
type debateRun struct {
	summary   debateSummary
	kill      func()
	cancelled bool
	timedOut  bool
	// done is closed by finish when set (code wiki builds wait on it to report an early CLI rejection).
	done chan struct{}
}

type debateHub struct {
	cfg  debateConfig
	mu   sync.Mutex
	runs map[string]*debateRun
	// actionRuns holds the action ids being executed right now (guarded by mu).
	actionRuns map[int]struct{}
	// planRuns holds the plan ids being run right now (guarded by mu).
	planRuns map[string]struct{}
	// soloRuns holds the solo run ids being run right now (guarded by mu; see solo_handler.go).
	soloRuns map[string]struct{}
	// reviewRuns holds the quick review ids being run right now (guarded by mu; see review_handler.go).
	reviewRuns map[string]struct{}
	// eventPoll and eventHeartbeat pace the follow stream (fields so tests can shorten them).
	eventPoll, eventHeartbeat time.Duration
	// closed and published cache the DB's CLOSED / published thread ids for the job list.
	closed, published threadSetCache
}

// threadSetTTL is how long GET /api/debates reuses a thread id set (CLOSED, published) read from the DB.
// The list is polled every few seconds; this keeps it to one CLI run per TTL and set.
const threadSetTTL = 30 * time.Second

// threadSetCache holds a set of thread ids from the decision DB. Threads closed outside the
// dashboard (CLI --close-thread) are only known there. Nothing closes a thread automatically:
// CLOSED is written only by --close-thread, which the close route also runs. The published set
// changes when a publish pushes (the publish route invalidates it) or the CLI publishes.
type threadSetCache struct {
	fetchMu sync.Mutex // one CLI run at a time; the others wait and reuse its result
	mu      sync.Mutex // guards the fields below
	ids     map[string]bool
	fetched time.Time
	gen     int // bumped by invalidate so a fetch that started earlier does not store stale ids
}

func (c *threadSetCache) invalidate() {
	c.mu.Lock()
	c.ids, c.gen = nil, c.gen+1
	c.mu.Unlock()
}

func (c *threadSetCache) fresh() (map[string]bool, int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.ids != nil && time.Since(c.fetched) < threadSetTTL {
		return c.ids, c.gen
	}
	return nil, c.gen
}

// closedThreads returns the CLOSED thread ids, or ok=false when the DB cannot be asked (no CLI,
// CLI error); the list then falls back to the thread_closed flag stored in each job's meta.
func (h *debateHub) closedThreads() (map[string]bool, bool) {
	return h.threadSet(&h.closed, "--closed-threads")
}

// publishedThreads returns the ids of threads with a PUSHED publication, or ok=false when the DB
// cannot be asked (the CLI may be older than this agent); callers then skip the overlay and the check.
func (h *debateHub) publishedThreads() (map[string]bool, bool) {
	return h.threadSet(&h.published, "--published-threads")
}

// threadSet reads {"threads": [...]} from `guardian.py <flag> --json` through the cache c.
func (h *debateHub) threadSet(c *threadSetCache, flag string) (map[string]bool, bool) {
	if ids, _ := c.fresh(); ids != nil {
		return ids, true
	}
	c.fetchMu.Lock()
	defer c.fetchMu.Unlock()
	ids, gen := c.fresh()
	if ids != nil {
		return ids, true
	}
	if st, err := os.Stat(h.cfg.CLI); err != nil || st.IsDir() {
		return nil, false
	}
	code, out, _, err := h.runConfigCLI(nil, flag, "--json")
	var resp struct {
		Threads []string `json:"threads"`
	}
	if err != nil || code != 0 || json.Unmarshal(out, &resp) != nil {
		return nil, false
	}
	ids = make(map[string]bool, len(resp.Threads))
	for _, t := range resp.Threads {
		ids[t] = true
	}
	c.mu.Lock()
	if c.gen == gen {
		c.ids, c.fetched = ids, time.Now()
	}
	c.mu.Unlock()
	return ids, true
}

func registerDebateRoutes(mux *http.ServeMux) {
	newDebateHub(debateConfigFromEnv()).register(mux)
}

func newDebateHub(cfg debateConfig) *debateHub {
	h := &debateHub{cfg: cfg, runs: map[string]*debateRun{}, actionRuns: map[int]struct{}{}, planRuns: map[string]struct{}{}, soloRuns: map[string]struct{}{},
		reviewRuns: map[string]struct{}{},
		eventPoll:  eventPollInterval, eventHeartbeat: eventHeartbeatInterval}
	h.markInterrupted()
	return h
}

func (h *debateHub) register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/personas", h.guard(h.configGet("--personas")))
	mux.HandleFunc("POST /api/personas", h.guard(h.configSave("--persona-save")))
	mux.HandleFunc("POST /api/personas/suggest", h.guard(h.personaSuggest))
	mux.HandleFunc("DELETE /api/personas/{id}", h.guard(h.configDelete("--persona-delete")))
	mux.HandleFunc("GET /api/teams", h.guard(h.configGet("--teams")))
	// Read-only: the model workers' probe records and a live ping per enabled host (the CLI pings, not this agent).
	mux.HandleFunc("GET /api/workers", h.guard(h.configGet("--workers")))
	// Read-only: resident agent monitor, docs/design/resident-monitor.md.
	mux.HandleFunc("GET /api/residents/monitor", h.guard(h.configGet("--resident-monitor")))
	mux.HandleFunc("GET /api/residents/mail/{id}", h.guard(h.residentMailBody))
	mux.HandleFunc("GET /api/residents/artifacts", h.guard(h.configGet("--resident-artifacts")))
	mux.HandleFunc("GET /api/residents/artifacts/{id}", h.guard(h.residentArtifactGet))
	mux.HandleFunc("POST /api/residents/queries", h.guard(h.residentQuerySubmit))
	mux.HandleFunc("GET /api/residents/queries", h.guard(h.residentQueryList))
	mux.HandleFunc("GET /api/residents/queries/{id}", h.guard(h.residentQueryGet))
	mux.HandleFunc("DELETE /api/residents/queries/{id}", h.guard(h.residentQueryCancel))
	mux.HandleFunc("GET /api/residents/sessions", h.guard(h.residentSessionList))
	mux.HandleFunc("POST /api/residents/sessions/reset", h.guard(h.residentSessionReset))
	mux.HandleFunc("POST /api/teams", h.guard(h.configSave("--team-save")))
	mux.HandleFunc("DELETE /api/teams/{id}", h.guard(h.configDelete("--team-delete")))
	mux.HandleFunc("GET /api/flow-templates", h.guard(h.configGet("--flow-templates")))
	mux.HandleFunc("POST /api/flow-templates", h.guard(h.configSave("--flow-template-save")))
	mux.HandleFunc("DELETE /api/flow-templates/{id}", h.guard(h.configDelete("--flow-template-delete")))
	mux.HandleFunc("GET /api/flows", h.guard(h.configGet("--flows")))
	mux.HandleFunc("POST /api/flows", h.guard(h.configSave("--flow-save")))
	mux.HandleFunc("DELETE /api/flows/{id}", h.guard(h.configDelete("--flow-delete")))
	mux.HandleFunc("GET /api/projects", h.guard(h.configGet("--projects")))
	mux.HandleFunc("POST /api/projects", h.guard(h.configSave("--project-save")))
	mux.HandleFunc("DELETE /api/projects/{id}", h.guard(h.configDelete("--project-delete")))
	mux.HandleFunc("POST /api/projects/{id}/sync", h.guard(h.syncProject))
	// Node groups (docs/design/node-groups.md): Python validates and resolves members; the hub
	// only forwards. Execution endpoints receive resolved server ids, never group names.
	// Package roles (docs/design/fleet-provisioning.md 6.2): the roles file, read-only; nodes pick roles by name.
	mux.HandleFunc("GET /api/package-roles", h.guard(h.configGet("--package-roles")))
	// Command sets (docs/design/batch-execution.md, B4c): the operator's own sets; builtin ones are read-only.
	mux.HandleFunc("GET /api/command-sets", h.guard(h.configGet("--command-sets")))
	mux.HandleFunc("GET /api/verdict-sources", h.guard(h.configGet("--verdict-sources")))
	mux.HandleFunc("POST /api/command-sets", h.guard(h.configSave("--command-set-save")))
	mux.HandleFunc("DELETE /api/command-sets/{id}", h.guard(h.configDelete("--command-set-delete")))
	// Confirming takes only the id (the CLI reads the stored text), so it shares configDelete's id-only shape.
	mux.HandleFunc("POST /api/command-sets/{id}/confirm", h.guard(h.configDelete("--command-set-confirm")))
	mux.HandleFunc("GET /api/groups", h.guard(h.configGet("--groups")))
	mux.HandleFunc("POST /api/groups", h.guard(h.configSave("--group-save")))
	mux.HandleFunc("POST /api/groups/preview", h.guard(h.configSave("--group-preview")))
	mux.HandleFunc("DELETE /api/groups/{id}", h.guard(h.configDelete("--group-delete")))
	mux.HandleFunc("POST /api/debates", h.guard(h.startDebate))
	mux.HandleFunc("GET /api/debates", h.listDebates)
	mux.HandleFunc("GET /api/debates/{id}", h.getDebate)
	mux.HandleFunc("GET /api/debates/{id}/events", h.debateEvents)
	mux.HandleFunc("GET /api/debates/{id}/prompts/{seq}", h.debatePrompt)
	mux.HandleFunc("GET /api/threads/{thread}/report", h.threadReport)
	mux.HandleFunc("GET /api/threads/{thread}/report/assets/{name}", h.threadReportAsset)
	mux.HandleFunc("DELETE /api/debates/{id}", h.cancelDebate)
	mux.HandleFunc("GET /api/actions", h.guard(h.listActions))
	mux.HandleFunc("GET /api/actions/{id}", h.guard(h.showAction))
	mux.HandleFunc("POST /api/actions/{id}/run", h.guard(h.runAction))
	mux.HandleFunc("POST /api/actions/{id}/reject", h.guard(h.rejectAction))
	mux.HandleFunc("POST /api/actions/{id}/fail-stale", h.guard(h.failStaleAction))
	h.registerPlanRoutes(mux)
	mux.HandleFunc("POST /api/threads/{thread}/jev-recheck", h.guard(h.jevRecheck))
	mux.HandleFunc("POST /api/threads/{thread}/close", h.guard(h.closeThread))
	mux.HandleFunc("POST /api/threads/{thread}/publish", h.guard(h.publishThread))
	mux.HandleFunc("GET /api/threads/{thread}/publish-plan", h.guard(h.publishPlan))
	mux.HandleFunc("GET /api/threads/{thread}/publications", h.guard(h.threadPublications))
	mux.HandleFunc("GET /api/threads/closed", h.guard(h.closedThreadList))
	mux.HandleFunc("GET /api/projects/{id}/workspace-reset", h.guard(h.workspaceResetPlan))
	mux.HandleFunc("POST /api/projects/{id}/workspace-reset", h.guard(h.workspaceReset))
	h.registerWikiRoutes(mux)
	h.registerArtifactRoutes(mux)
	h.registerResultRoutes(mux)
	h.registerSoloRoutes(mux)
	h.registerReviewRoutes(mux)
}

// guard returns 503 when the CLI is missing on this host. CORS is owned by withSecurity.
func (h *debateHub) guard(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if st, err := os.Stat(h.cfg.CLI); err != nil || st.IsDir() {
			jsonResponse(w, http.StatusServiceUnavailable, map[string]string{"error": debateUnavailableMsg})
			return
		}
		next(w, r)
	}
}

func errJSON(w http.ResponseWriter, status int, msg string) {
	jsonResponse(w, status, map[string]string{"error": msg})
}

// ---- persona / team config ----

// @MX:WARN: [AUTO] Spawns the Python debate CLI, which in turn runs LLM CLIs (claude, codex, ...) on the hub.
// @MX:REASON: Inputs reach the child only as argv entries or stdin (never a shell); the child gets its own process group so cancel/timeout kill the grandchildren too.
func (h *debateHub) newCmd(ctx context.Context, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, h.cfg.Python, append([]string{h.cfg.CLI}, args...)...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = 2 * time.Second
	cmd.Env = append(os.Environ(), "PYTHONUNBUFFERED=1")
	if path, err := serversFile(); err == nil {
		// Same inventory for node group membership as the agent's own fleet endpoints.
		cmd.Env = append(cmd.Env, "GUARDIAN_SERVERS_FILE="+path)
	}
	return cmd
}

// runConfigCLI runs a short config command and returns exit code, stdout and stderr.
func (h *debateHub) runConfigCLI(stdin []byte, args ...string) (int, []byte, string, error) {
	return h.runCLI(h.cfg.ConfigLimit, stdin, args...)
}

// runCLI runs the CLI to completion with its own timeout. The context is deliberately not
// derived from any HTTP request, so a client disconnect never kills the child.
func (h *debateHub) runCLI(timeout time.Duration, stdin []byte, args ...string) (int, []byte, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := h.newCmd(ctx, args...)
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	var ee *exec.ExitError
	switch {
	case err == nil:
		return 0, stdout.Bytes(), stderr.String(), nil
	case ctx.Err() != nil:
		return -1, stdout.Bytes(), stderr.String(), fmt.Errorf("timed out after %s", timeout)
	case errors.As(err, &ee):
		return ee.ExitCode(), stdout.Bytes(), stderr.String(), nil
	default:
		return -1, nil, "", err
	}
}

func (h *debateHub) respondConfig(w http.ResponseWriter, code int, stdout []byte, stderr string, err error) {
	if err != nil {
		errJSON(w, http.StatusInternalServerError, "debate engine failed: "+err.Error())
		return
	}
	switch code {
	case 0:
		var probe map[string]json.RawMessage
		if json.Unmarshal(stdout, &probe) != nil {
			errJSON(w, http.StatusInternalServerError, "debate engine returned invalid JSON: "+tailString(strings.TrimSpace(stderr), errLogTailBytes))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(stdout)
	case 2, 3, 4:
		status := map[int]int{2: http.StatusBadRequest, 3: http.StatusNotFound, 4: http.StatusConflict}[code]
		var out struct {
			Error string `json:"error"`
		}
		msg := ""
		if json.Unmarshal(stdout, &out) == nil {
			msg = out.Error
		}
		if msg == "" {
			// argparse also exits 2 on unknown flags, e.g. when guardian.py is older than this agent.
			msg = joinErr("validation failed", tailString(strings.TrimSpace(stderr), errLogTailBytes))
			if code != 2 {
				msg = joinErr(fmt.Sprintf("debate engine exited with code %d", code), tailString(strings.TrimSpace(stderr), errLogTailBytes))
			}
		}
		errJSON(w, status, msg)
	default:
		errJSON(w, http.StatusInternalServerError, fmt.Sprintf("debate engine exited with code %d: %s", code, tailString(strings.TrimSpace(stderr), errLogTailBytes)))
	}
}

func (h *debateHub) configGet(flag string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		code, out, errOut, err := h.runConfigCLI(nil, flag, "--json")
		h.respondConfig(w, code, out, errOut, err)
	}
}

func (h *debateHub) configSave(flag string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, configBodyLimit))
		if err != nil {
			var mbe *http.MaxBytesError
			if errors.As(err, &mbe) {
				errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
				return
			}
			errJSON(w, http.StatusBadRequest, "cannot read request body")
			return
		}
		if len(bytes.TrimSpace(body)) == 0 {
			errJSON(w, http.StatusBadRequest, "request body required")
			return
		}
		code, out, errOut, err := h.runConfigCLI(body, flag, "--json")
		h.respondConfig(w, code, out, errOut, err)
	}
}

func (h *debateHub) configDelete(flag string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		if !idPattern.MatchString(id) {
			errJSON(w, http.StatusBadRequest, "invalid id")
			return
		}
		code, out, errOut, err := h.runConfigCLI(nil, flag, id, "--json")
		h.respondConfig(w, code, out, errOut, err)
	}
}

// residentMailBody returns one resident mail's body (docs/design/resident-monitor.md). The id is
// checked before the CLI runs, so a bad id never spawns a process.
func (h *debateHub) residentMailBody(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !mailIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid mail id")
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--resident-mail-body", id, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// residentArtifactGet returns one resident artifact's metadata+content as JSON, or raw bytes when
// ?download=1 is passed (docs/design/resident-codex-and-policy.md, PD4).
func (h *debateHub) residentArtifactGet(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !artifactIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid artifact id")
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--resident-artifact-get", id, "--json")
	if r.URL.Query().Get("download") != "1" || err != nil || code != 0 {
		h.respondConfig(w, code, out, errOut, err)
		return
	}
	var payload struct {
		Filename   string `json:"filename"`
		Mime       string `json:"mime"`
		ContentB64 string `json:"content_b64"`
		Missing    bool   `json:"missing"`
	}
	if jerr := json.Unmarshal(out, &payload); jerr != nil || payload.Missing || payload.ContentB64 == "" {
		errJSON(w, http.StatusNotFound, "artifact content unavailable")
		return
	}
	raw, berr := base64.StdEncoding.DecodeString(payload.ContentB64)
	if berr != nil {
		errJSON(w, http.StatusInternalServerError, "invalid artifact encoding")
		return
	}
	name := filepath.Base(payload.Filename)
	if name == "" || name == "." || name == "/" {
		name = id + ".bin"
	}
	mime := payload.Mime
	if mime == "" {
		mime = "application/octet-stream"
	}
	w.Header().Set("Content-Type", mime)
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", name))
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

const residentQueryBodyLimit = 40 << 10

// residentQuerySubmit queues one project-scoped direct query to an active resident session
// (docs/design/resident-session-query.md, QD1-QD4). Returns 202 Accepted on success.
func (h *debateHub) residentQuerySubmit(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, residentQueryBodyLimit))
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "cannot read request body")
		return
	}
	var req struct {
		Address    string `json:"address"`
		Project    string `json:"project"`
		Prompt     string `json:"prompt"`
		SessionKey string `json:"session_key"`
		Mode       string `json:"mode"`
		Task       string `json:"task"`
		TimeoutSec int    `json:"timeout_sec"`
	}
	if jerr := json.Unmarshal(body, &req); jerr != nil {
		errJSON(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if !residentAddrPattern.MatchString(req.Address) {
		errJSON(w, http.StatusBadRequest, "invalid resident address")
		return
	}
	if !idPattern.MatchString(req.Project) {
		errJSON(w, http.StatusBadRequest, "invalid project id")
		return
	}
	if strings.TrimSpace(req.Prompt) == "" {
		errJSON(w, http.StatusBadRequest, "prompt is required")
		return
	}
	if len(req.Prompt) > 32*1024 {
		errJSON(w, http.StatusBadRequest, "prompt exceeds 32 KiB")
		return
	}
	args := []string{"--resident-query", req.Address, "--project", req.Project, "--prompt-stdin", "--no-wait", "--json"}
	if req.SessionKey != "" {
		if !sessionKeyPattern.MatchString(req.SessionKey) {
			errJSON(w, http.StatusBadRequest, "invalid session_key")
			return
		}
		args = append(args, "--query-session-key", req.SessionKey)
	}
	if req.Mode != "" {
		if req.Mode != "in_place" && req.Mode != "fork" {
			errJSON(w, http.StatusBadRequest, "mode must be in_place or fork")
			return
		}
		args = append(args, "--query-mode", req.Mode)
	}
	if req.Task != "" {
		if !idPattern.MatchString(req.Task) {
			errJSON(w, http.StatusBadRequest, "invalid task id")
			return
		}
		args = append(args, "--query-task", req.Task)
	}
	if req.TimeoutSec != 0 {
		if req.TimeoutSec < 10 || req.TimeoutSec > 600 {
			errJSON(w, http.StatusBadRequest, "timeout_sec must be between 10 and 600")
			return
		}
		args = append(args, "--query-timeout", strconv.Itoa(req.TimeoutSec))
	}
	code, out, errOut, runErr := h.runConfigCLI([]byte(req.Prompt), args...)
	if runErr == nil && code == 0 && json.Valid(out) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write(out)
		return
	}
	h.respondConfig(w, code, out, errOut, runErr)
}

// residentQueryList lists recent resident session query metadata (no prompt/answer bodies).
func (h *debateHub) residentQueryList(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	args := []string{"--resident-queries"}
	if agent := q.Get("agent"); agent != "" {
		if !residentAddrPattern.MatchString(agent) {
			errJSON(w, http.StatusBadRequest, "invalid resident address")
			return
		}
		args = append(args, agent)
	}
	if proj := q.Get("project"); proj != "" {
		if !idPattern.MatchString(proj) {
			errJSON(w, http.StatusBadRequest, "invalid project id")
			return
		}
		args = append(args, "--project", proj)
	}
	if skey := q.Get("session_key"); skey != "" {
		if !sessionKeyPattern.MatchString(skey) {
			errJSON(w, http.StatusBadRequest, "invalid session_key")
			return
		}
		args = append(args, "--query-session-key", skey)
	}
	if rawLimit := q.Get("limit"); rawLimit != "" {
		n, err := strconv.Atoi(rawLimit)
		if err != nil || n < 1 || n > 200 {
			errJSON(w, http.StatusBadRequest, "limit must be between 1 and 200")
			return
		}
		args = append(args, "--query-limit", strconv.Itoa(n))
	}
	args = append(args, "--json")
	code, out, errOut, err := h.runConfigCLI(nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

// residentQueryGet returns one resident query with masked prompt & answer (mode=ro).
func (h *debateHub) residentQueryGet(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !queryIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid query id")
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--resident-query-get", id, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// residentQueryCancel cancels a queued/dispatched resident query.
func (h *debateHub) residentQueryCancel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !queryIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid query id")
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--resident-query-cancel", id, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// residentSessionList lists scoped active sessions for resident agents (mode=ro).
func (h *debateHub) residentSessionList(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	args := []string{"--resident-sessions"}
	if agent := q.Get("agent"); agent != "" {
		if !residentAddrPattern.MatchString(agent) {
			errJSON(w, http.StatusBadRequest, "invalid resident address")
			return
		}
		args = append(args, agent)
	}
	if proj := q.Get("project"); proj != "" {
		if !idPattern.MatchString(proj) {
			errJSON(w, http.StatusBadRequest, "invalid project id")
			return
		}
		args = append(args, "--project", proj)
	}
	if q.Get("include_inactive") == "1" || q.Get("include_inactive") == "true" {
		args = append(args, "--include-inactive")
	}
	args = append(args, "--json")
	code, out, errOut, err := h.runConfigCLI(nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

// residentSessionReset archives/resets one scoped session on a resident agent.
func (h *debateHub) residentSessionReset(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, actionBodyLimit))
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "cannot read request body")
		return
	}
	var req struct {
		Address    string `json:"address"`
		SessionKey string `json:"session_key"`
	}
	if jerr := json.Unmarshal(body, &req); jerr != nil {
		errJSON(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if !residentAddrPattern.MatchString(req.Address) {
		errJSON(w, http.StatusBadRequest, "invalid resident address")
		return
	}
	if !sessionKeyPattern.MatchString(req.SessionKey) || req.SessionKey == "main" {
		errJSON(w, http.StatusBadRequest, "invalid scoped session_key")
		return
	}
	code, out, errOut, runErr := h.runConfigCLI(
		nil, "--resident-session-reset", req.Address, "--session-key", req.SessionKey, "--json",
	)
	h.respondConfig(w, code, out, errOut, runErr)
}

// syncProject clones or fast-forwards a project's workspace on the hub. A clone can take a
// while, so it gets the action timeout rather than the short config one.
func (h *debateHub) syncProject(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !idPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	code, out, errOut, err := h.runCLI(h.cfg.actionTimeout(), nil, "--project-sync", id, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// projectOrDefault treats jobs started before projects existed as the default project.
func (s debateSummary) projectOrDefault() string {
	if s.Project == "" {
		return defaultProjectID
	}
	return s.Project
}

// ---- debate jobs ----

type debateRequest struct {
	Task     string `json:"task"`
	Team     string `json:"team"`
	ThreadID string `json:"thread_id"`
	Rounds   *int   `json:"rounds"`
	// Project selects the repository (configs/projects) the debate reads and its actions change.
	Project string `json:"project"`
	// ContextPaths are globs of committed files for the agents to read; ContextRef is the git ref.
	ContextPaths []string `json:"context_paths"`
	ContextRef   string   `json:"context_ref"`
	// Template starts a flow template (configs/flow-templates) instead of a free task; Params fill its
	// ${NAME} placeholders. Python is the only validator of the values; Go bounds their shape.
	Template string            `json:"template"`
	Params   map[string]string `json:"params"`
	// Flow starts a flow (docs/design/flow-graph.md): a stored flow, or a template or team converted. Seats swap
	// characters into its seats (seat id or "close" -> character id). A flow seats its own team.
	Flow  string            `json:"flow"`
	Seats map[string]string `json:"seats"`
}

const (
	maxTemplateParams     = 8
	maxTemplateParamChars = 4000
)

var (
	paramNamePattern = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,39}$`)
	seatIDPattern    = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,31}$`)
)

const maxFlowSeats = 16

// validateFlow checks the flow fields of a start request: a flow seats its own characters (no team, no template)
// and starts a new thread; seats mean nothing without a flow. The task is optional (a flow may have its own text).
func (req *debateRequest) validateFlow() string {
	if req.Flow == "" {
		if len(req.Seats) > 0 {
			return "seats need a flow"
		}
		return ""
	}
	switch {
	case !idPattern.MatchString(req.Flow):
		return "invalid flow"
	case req.Template != "":
		return "start either a flow or a template"
	case req.Team != "":
		return "a flow seats its own characters; drop team (use seats)"
	case req.ThreadID != "":
		return "a flow starts a new thread; drop thread_id"
	case len(req.Seats) > maxFlowSeats:
		return fmt.Sprintf("seats must have at most %d entries", maxFlowSeats)
	}
	for seat, character := range req.Seats {
		if !seatIDPattern.MatchString(seat) {
			return fmt.Sprintf("invalid seat %q", seat)
		}
		if !idPattern.MatchString(character) {
			return fmt.Sprintf("invalid character for seat %s", seat)
		}
	}
	return ""
}

// validateTemplate checks the template fields of a start request: a template supplies the task and starts
// a new thread, and params mean nothing without one.
func (req *debateRequest) validateTemplate() string {
	if req.Template == "" {
		if len(req.Params) > 0 && req.Flow == "" {
			return "params need a template or a flow"
		}
		return checkParams(req.Params)
	}
	switch {
	case !idPattern.MatchString(req.Template):
		return "invalid template"
	case req.Task != "":
		return "a template supplies the task; drop task"
	case req.ThreadID != "":
		return "a template starts a new thread; drop thread_id"
	}
	return checkParams(req.Params)
}

// checkParams bounds the shape of template and flow parameters; Python validates the values.
func checkParams(params map[string]string) string {
	if len(params) > maxTemplateParams {
		return fmt.Sprintf("params must have at most %d entries", maxTemplateParams)
	}
	for name, value := range params {
		switch {
		case !paramNamePattern.MatchString(name):
			return fmt.Sprintf("invalid param name %q", name)
		case utf8.RuneCountInString(value) > maxTemplateParamChars:
			return fmt.Sprintf("param %s must be at most %d characters", name, maxTemplateParamChars)
		case strings.ContainsRune(value, 0):
			return fmt.Sprintf("param %s contains invalid characters", name)
		}
	}
	return ""
}

func (req *debateRequest) validate() string {
	if msg := req.validateTemplate(); msg != "" {
		return msg
	}
	if msg := req.validateFlow(); msg != "" {
		return msg
	}
	if req.Template == "" && req.Flow == "" && strings.TrimSpace(req.Task) == "" {
		return "task required"
	}
	if utf8.RuneCountInString(req.Task) > maxTaskChars {
		return fmt.Sprintf("task must be at most %d characters", maxTaskChars)
	}
	if strings.ContainsRune(req.Task, 0) {
		return "task contains invalid characters"
	}
	if req.Team != "" && !idPattern.MatchString(req.Team) {
		return "invalid team"
	}
	if req.Project != "" && !idPattern.MatchString(req.Project) {
		return "invalid project"
	}
	if req.ThreadID != "" && !threadPattern.MatchString(req.ThreadID) {
		return "invalid thread_id"
	}
	if req.Rounds != nil && (*req.Rounds < 1 || *req.Rounds > maxDebateRounds) {
		return fmt.Sprintf("rounds must be between 1 and %d", maxDebateRounds)
	}
	if len(req.ContextPaths) > maxContextPaths {
		return fmt.Sprintf("context_paths must have at most %d entries", maxContextPaths)
	}
	for i, p := range req.ContextPaths {
		n := utf8.RuneCountInString(p)
		switch {
		case n < 1 || n > maxContextPathChars:
			return fmt.Sprintf("context_paths[%d] must be 1 to %d characters", i, maxContextPathChars)
		case strings.ContainsAny(p, "\x00\n\r"):
			return fmt.Sprintf("context_paths[%d] contains invalid characters", i)
		case strings.HasPrefix(p, "-"):
			return fmt.Sprintf("context_paths[%d] must not start with '-'", i)
		}
	}
	if req.ContextRef != "" && (strings.HasPrefix(req.ContextRef, "-") || !contextRefRe.MatchString(req.ContextRef)) {
		return "invalid context_ref"
	}
	return ""
}

func (h *debateHub) jobsDir() string { return filepath.Join(h.cfg.DataDir, "debate-jobs") }

func newJobID() string {
	var b [3]byte
	_, _ = rand.Read(b[:])
	return "dbt_" + time.Now().UTC().Format("20060102_150405") + "_" + hex.EncodeToString(b[:])
}

func (h *debateHub) startDebate(w http.ResponseWriter, r *http.Request) {
	var req debateRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, debateBodyLimit)).Decode(&req); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	if msg := req.validate(); msg != "" {
		errJSON(w, http.StatusBadRequest, msg)
		return
	}
	if msg := h.publishedRefusal(req.ThreadID); msg != "" {
		errJSON(w, http.StatusConflict, msg)
		return
	}

	run := &debateRun{summary: debateSummary{
		ID:        newJobID(),
		Task:      req.Task,
		Project:   req.Project,
		Team:      req.Team,
		ThreadID:  req.ThreadID,
		Status:    "running",
		StartedAt: time.Now().UTC(),

		ContextPaths: req.ContextPaths,
		ContextRef:   req.ContextRef,
		Template:     req.Template,
		Params:       req.Params,
		Flow:         req.Flow,
		Seats:        req.Seats,
	}}
	args := []string{"--debate=" + req.Task}
	if req.Template != "" {
		// "-" tells the CLI the task comes from the template; the rendered text returns in result.json.
		run.summary.Task = "template: " + req.Template
		args = []string{"--debate=-", "--flow-template=" + req.Template}
		names := make([]string, 0, len(req.Params))
		for name := range req.Params {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			args = append(args, "--param="+name+"="+req.Params[name])
		}
	}
	if req.Flow != "" {
		// Without a task the flow's own text is the task ("-"); the rendered text returns in result.json.
		if strings.TrimSpace(req.Task) == "" {
			run.summary.Task = "flow: " + req.Flow
			args = []string{"--debate=-"}
		}
		args = append(args, "--flow="+req.Flow)
		args = append(args, sortedPairs("--param=", req.Params)...)
		args = append(args, sortedPairs("--seat=", req.Seats)...)
	}
	if req.Team != "" {
		args = append(args, "--team", req.Team)
	}
	if req.Project != "" {
		args = append(args, "--project="+req.Project)
	}
	if req.ThreadID != "" {
		args = append(args, "--thread", req.ThreadID)
	}
	if req.Rounds != nil {
		run.summary.Rounds = *req.Rounds
		args = append(args, "--rounds", strconv.Itoa(*req.Rounds))
	}
	// Single "=" argv entries so a value can never be parsed as a separate flag.
	for _, p := range req.ContextPaths {
		args = append(args, "--context-path="+p)
	}
	if req.ContextRef != "" {
		args = append(args, "--context-ref="+req.ContextRef)
	}

	// Reserve the slot and register the job atomically so neither limit can be raced. Two debates on one
	// thread would overwrite each other's transcript (save_debate_session replaces all messages).
	h.mu.Lock()
	if req.ThreadID != "" {
		for _, other := range h.runs {
			if other.summary.ThreadID == req.ThreadID && other.summary.Status == "running" {
				h.mu.Unlock()
				errJSON(w, http.StatusConflict, "a debate is already running for this thread; wait for it to finish or cancel it")
				return
			}
		}
	}
	if h.runningDebatesLocked() >= h.cfg.MaxRunning {
		h.mu.Unlock()
		errJSON(w, http.StatusTooManyRequests, fmt.Sprintf("too many running debates (max %d)", h.cfg.MaxRunning))
		return
	}
	h.runs[run.summary.ID] = run
	h.mu.Unlock()

	if err := h.launch(run, args); err != nil {
		h.mu.Lock()
		delete(h.runs, run.summary.ID)
		h.mu.Unlock()
		errJSON(w, http.StatusInternalServerError, "cannot start debate: "+err.Error())
		return
	}
	h.mu.Lock()
	snapshot := run.summary
	h.mu.Unlock()
	jsonResponse(w, http.StatusAccepted, snapshot)
}

// runningDebatesLocked counts the live debate jobs; code wiki builds have their own slot. Callers hold h.mu.
func (h *debateHub) runningDebatesLocked() int {
	n := 0
	for _, r := range h.runs {
		if r.summary.Kind != kindCodeWiki {
			n++
		}
	}
	return n
}

// publishedRefusal returns the 409 text when thread was published (D11: a published thread is final), else "".
// The cached published-thread set decides; the branch for the message comes from `--publications`. Both are
// best effort: when the CLI cannot be asked the debate starts and the CLI itself refuses (exit 4) at validation.
func (h *debateHub) publishedRefusal(thread string) string {
	if thread == "" {
		return ""
	}
	if published, ok := h.publishedThreads(); !ok || !published[thread] {
		return ""
	}
	code, out, _, err := h.runConfigCLI(nil, "--publications", thread, "--json")
	var resp struct {
		Publications []struct {
			Branch string `json:"branch"`
			Status string `json:"status"`
		} `json:"publications"`
	}
	if err != nil || code != 0 || json.Unmarshal(out, &resp) != nil {
		return ""
	}
	for _, p := range resp.Publications { // newest first
		if p.Status == "PUSHED" {
			return fmt.Sprintf("thread %s was published as branch %s; start a new thread", thread, p.Branch)
		}
	}
	return ""
}

// launch creates the job directory, starts the process and the goroutine that waits for it.
// h.runs must already contain run, which counts toward the concurrency limit.
func (h *debateHub) launch(run *debateRun, args []string) error {
	dir := filepath.Join(h.jobsDir(), run.summary.ID)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	logFile, err := os.OpenFile(filepath.Join(dir, "output.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	resultPath := filepath.Join(dir, "result.json")
	// Single "=" entry for the events file, like the other path-valued flags.
	args = append([]string{"--result-file", resultPath, "--events-file=" + filepath.Join(dir, eventsFileName)}, args...)

	cmd := h.newCmd(context.Background(), args...)
	cmd.Stdout, cmd.Stderr = logFile, logFile
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return err
	}
	pid := cmd.Process.Pid
	kill := func() { _ = syscall.Kill(-pid, syscall.SIGKILL) }

	// run is already visible to cancelDebate, so kill is published under the lock, and a
	// cancel that arrived before the process existed is carried out now.
	h.mu.Lock()
	run.kill = kill
	cancelled := run.cancelled
	h.writeMetaLocked(run.summary)
	h.mu.Unlock()
	if cancelled {
		kill()
	}

	timer := time.AfterFunc(h.cfg.JobTimeout, func() {
		h.mu.Lock()
		run.timedOut = true
		h.mu.Unlock()
		run.kill()
	})
	go func() {
		waitErr := cmd.Wait()
		timer.Stop()
		run.kill() // reap any grandchildren left behind
		logFile.Close()
		h.finish(run, waitErr, dir)
	}()
	return nil
}

// finish records the final state of a job once its process has exited.
func (h *debateHub) finish(run *debateRun, waitErr error, dir string) {
	exit := 0
	if waitErr != nil {
		exit = -1
		var ee *exec.ExitError
		if errors.As(waitErr, &ee) {
			exit = ee.ExitCode()
		}
	}
	tail := ""
	if exit != 0 {
		tail = tailString(strings.TrimSpace(readTail(filepath.Join(dir, "output.log"), errLogTailBytes)), errLogTailBytes)
	}
	var result struct {
		ThreadID string `json:"thread_id"`
		Task     string `json:"task"`
	}
	resultOK := false
	if data, err := os.ReadFile(filepath.Join(dir, "result.json")); err == nil {
		resultOK = json.Unmarshal(data, &result) == nil
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	s := &run.summary
	now := time.Now().UTC()
	s.FinishedAt = &now
	s.ExitCode = &exit
	switch {
	case run.cancelled:
		// status and error were set by cancel
	case run.timedOut:
		s.Status, s.Error = "failed", fmt.Sprintf("timed out after %s", h.cfg.JobTimeout)
	case exit == 0 && resultOK:
		s.Status = "completed"
		if s.ThreadID == "" {
			s.ThreadID = result.ThreadID
		}
		if (s.Template != "" || s.Flow != "") && result.Task != "" {
			s.Task = result.Task
		}
		if s.Kind != kindCodeWiki {
			// A finished debate sets the thread's status again, which reopens a CLOSED thread.
			h.setThreadClosedLocked(s.ThreadID, false, s.ID)
			h.closed.invalidate()
		}
	case exit == 2:
		// Exit 2: the team, project or code context was rejected before the debate started.
		s.Status, s.Error = "failed", joinErr("validation failed", tail)
	case exit == 0:
		s.Status, s.Error = "failed", s.noun()+" finished without a valid result file"
	default:
		s.Status, s.Error = "failed", joinErr(fmt.Sprintf("%s exited with code %d", s.noun(), exit), tail)
	}
	h.writeMetaLocked(*s)
	delete(h.runs, s.ID)
	if run.done != nil {
		close(run.done)
	}
}

// noun names the job for error texts.
func (s debateSummary) noun() string {
	if s.Kind == kindCodeWiki {
		return "code wiki build"
	}
	return "debate"
}

func joinErr(msg, tail string) string {
	if tail == "" {
		return msg
	}
	return msg + ": " + tail
}

func (h *debateHub) cancelDebate(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !jobIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	h.mu.Lock()
	run, ok := h.runs[id]
	if ok && run.summary.Status == "running" && !run.cancelled {
		run.cancelled = true
		now := time.Now().UTC()
		run.summary.Status = "cancelled"
		run.summary.FinishedAt = &now
		run.summary.Error = "cancelled by user"
		h.writeMetaLocked(run.summary)
		snapshot := run.summary
		kill := run.kill
		h.mu.Unlock()
		if kill != nil { // nil until launch has started the process; launch then kills it
			kill()
		}
		jsonResponse(w, http.StatusOK, snapshot)
		return
	}
	h.mu.Unlock()
	if _, err := h.readMeta(id); err != nil {
		errJSON(w, http.StatusNotFound, "debate not found")
		return
	}
	errJSON(w, http.StatusConflict, "debate already finished")
}

func (h *debateHub) getDebate(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !jobIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	sum, err := h.readMeta(id)
	if err != nil {
		errJSON(w, http.StatusNotFound, "debate not found")
		return
	}
	dir := filepath.Join(h.jobsDir(), id)
	detail := debateDetail{debateSummary: sum, Log: readTail(filepath.Join(dir, "output.log"), logTailBytes)}
	if data, err := os.ReadFile(filepath.Join(dir, "result.json")); err == nil && json.Valid(data) {
		detail.Result = json.RawMessage(data)
		detail.Retrieval = h.readRetrieval(id)
	}
	jsonResponse(w, http.StatusOK, detail)
}

func (h *debateHub) listDebates(w http.ResponseWriter, r *http.Request) {
	q, qerr := url.ParseQuery(r.URL.RawQuery)
	if qerr != nil {
		errJSON(w, http.StatusBadRequest, "invalid query string")
		return
	}
	project := q.Get("project")
	if project != "" && !idPattern.MatchString(project) {
		errJSON(w, http.StatusBadRequest, "invalid project")
		return
	}
	entries, _ := os.ReadDir(h.jobsDir())
	jobs := make([]debateSummary, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() || !jobIDPattern.MatchString(e.Name()) {
			continue
		}
		if sum, err := h.readMeta(e.Name()); err == nil && (project == "" || sum.projectOrDefault() == project) {
			jobs = append(jobs, sum)
		}
	}
	// The DB decides whether a thread is closed, however it was closed; running jobs keep their
	// own flag because a debate in progress reopens its thread only when it finishes.
	if closed, ok := h.closedThreads(); ok {
		for i := range jobs {
			if jobs[i].ThreadID != "" && jobs[i].Status != "running" {
				jobs[i].ThreadClosed = closed[jobs[i].ThreadID]
			}
		}
	}
	// Published threads are final; the same overlay as thread_closed, from the DB.
	if published, ok := h.publishedThreads(); ok {
		for i := range jobs {
			if jobs[i].ThreadID != "" && jobs[i].Status != "running" {
				jobs[i].ThreadPublished = published[jobs[i].ThreadID]
			}
		}
	}
	sort.Slice(jobs, func(i, j int) bool {
		if !jobs[i].StartedAt.Equal(jobs[j].StartedAt) {
			return jobs[i].StartedAt.After(jobs[j].StartedAt)
		}
		return jobs[i].ID > jobs[j].ID
	})
	if len(jobs) > listLimit {
		jobs = jobs[:listLimit]
	}
	items := make([]debateListItem, len(jobs))
	for i, j := range jobs {
		items[i] = debateListItem{debateSummary: j, Events: h.hasEvents(j.ID), Retrieval: h.readRetrieval(j.ID)}
	}
	jsonResponse(w, http.StatusOK, map[string]interface{}{"debates": items})
}

// ---- persistence ----

func (h *debateHub) readMeta(id string) (debateSummary, error) {
	var sum debateSummary
	data, err := os.ReadFile(filepath.Join(h.jobsDir(), id, "meta.json"))
	if err != nil {
		return sum, err
	}
	return sum, json.Unmarshal(data, &sum)
}

// writeMetaLocked rewrites meta.json atomically (temp file + rename). Callers hold h.mu so
// writes of one job land in state-change order.
func (h *debateHub) writeMetaLocked(s debateSummary) {
	dir := filepath.Join(h.jobsDir(), s.ID)
	if err := writeJSONAtomic(filepath.Join(dir, "meta.json"), s); err != nil {
		log.Printf("debate %s: cannot write meta.json: %v", s.ID, err)
	}
}

func writeJSONAtomic(path string, v interface{}) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".meta-*.tmp")
	if err != nil {
		return err
	}
	_, werr := tmp.Write(data)
	cerr := tmp.Close()
	if werr != nil || cerr != nil {
		os.Remove(tmp.Name())
		return errors.Join(werr, cerr)
	}
	if err := os.Chmod(tmp.Name(), 0o600); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	return nil
}

// markInterrupted flags jobs left "running" by a previous agent process.
func (h *debateHub) markInterrupted() {
	entries, _ := os.ReadDir(h.jobsDir())
	for _, e := range entries {
		if !e.IsDir() || !jobIDPattern.MatchString(e.Name()) {
			continue
		}
		sum, err := h.readMeta(e.Name())
		if err != nil || sum.Status != "running" {
			continue
		}
		now := time.Now().UTC()
		sum.Status, sum.FinishedAt, sum.Error = "interrupted", &now, "agent restarted while the "+sum.noun()+" was running"
		h.mu.Lock()
		h.writeMetaLocked(sum)
		h.mu.Unlock()
	}
}

// readTail returns at most max bytes from the end of a file (empty when unreadable).
func readTail(path string, max int64) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return ""
	}
	off := int64(0)
	if st.Size() > max {
		off = st.Size() - max
	}
	buf := make([]byte, st.Size()-off)
	n, _ := f.ReadAt(buf, off)
	return strings.ToValidUTF8(string(buf[:n]), "")
}

func tailString(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return strings.ToValidUTF8(s[len(s)-max:], "")
}

// ---- judge actions ----

// parseActionID accepts only canonical positive decimals in 1..2^31-1 (no sign, no leading zeros).
func parseActionID(raw string) (int, bool) {
	if !actionIDForm.MatchString(raw) {
		return 0, false
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 || n > maxActionID {
		return 0, false
	}
	return n, true
}

// actorFor builds the audit identity passed as --actor from the connection's remote host.
// It never uses client-supplied data; the result always matches ^[A-Za-z0-9@:._-]{1,64}$.
func actorFor(remoteAddr string) string {
	actor := "dashboard"
	if host, _, err := net.SplitHostPort(remoteAddr); err == nil && host != "" {
		actor = "dashboard@" + host
	}
	actor = actorUnsafe.ReplaceAllString(actor, "_")
	if len(actor) > 64 {
		actor = actor[:64]
	}
	return actor
}

// actionIDParam validates the {id} path value, writing 400 on failure.
func actionIDParam(w http.ResponseWriter, r *http.Request) (int, bool) {
	id, ok := parseActionID(r.PathValue("id"))
	if !ok {
		errJSON(w, http.StatusBadRequest, "invalid action id")
	}
	return id, ok
}

func (h *debateHub) listActions(w http.ResponseWriter, r *http.Request) {
	args := []string{"--actions"}
	// url.Values via Query() silently drops pairs with ';', which would widen the filter; reject instead.
	q, qerr := url.ParseQuery(r.URL.RawQuery)
	if qerr != nil {
		errJSON(w, http.StatusBadRequest, "invalid query string")
		return
	}
	if thread := q.Get("thread"); thread != "" {
		if !threadPattern.MatchString(thread) {
			errJSON(w, http.StatusBadRequest, "invalid thread")
			return
		}
		args = append(args, thread)
	}
	if project := q.Get("project"); project != "" {
		if !idPattern.MatchString(project) {
			errJSON(w, http.StatusBadRequest, "invalid project")
			return
		}
		args = append(args, "--project="+project)
	}
	code, out, errOut, err := h.runConfigCLI(nil, append(args, "--json")...)
	h.respondConfig(w, code, out, errOut, err)
}

func (h *debateHub) showAction(w http.ResponseWriter, r *http.Request) {
	id, ok := actionIDParam(w, r)
	if !ok {
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--show-action", strconv.Itoa(id), "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// uiActionsBlocked answers 403 and returns true when GUARDIAN_UI_ACTIONS=off. what names the blocked
// operation and cli the equivalent CLI command the operator can run on the hub instead.
func (h *debateHub) uiActionsBlocked(w http.ResponseWriter, what, cli string) bool {
	if !h.cfg.actionsDisabled() {
		return false
	}
	errJSON(w, http.StatusForbidden, what+" from the dashboard is disabled on this hub (GUARDIAN_UI_ACTIONS=off); use the CLI: guardian.py "+cli)
	return true
}

func (h *debateHub) rejectAction(w http.ResponseWriter, r *http.Request) {
	id, ok := actionIDParam(w, r)
	if !ok {
		return
	}
	if h.uiActionsBlocked(w, "rejecting actions", "--reject-action "+strconv.Itoa(id)) {
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--reject-action", strconv.Itoa(id), "--json", "--actor", actorFor(r.RemoteAddr))
	h.respondConfig(w, code, out, errOut, err)
}

func (h *debateHub) jevRecheck(w http.ResponseWriter, r *http.Request) {
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	if h.uiActionsBlocked(w, "re-checking a thread", "--jev-recheck "+thread) {
		return
	}
	code, out, errOut, err := h.runCLI(jevRecheckTimeout, nil, "--jev-recheck", thread, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// closeThread sets a thread CLOSED: its records stay, dashboards hide it, and a new debate on the
// same thread reopens it. Refused while a debate for the thread is running here.
func (h *debateHub) closeThread(w http.ResponseWriter, r *http.Request) {
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	if h.uiActionsBlocked(w, "closing a thread", "--close-thread "+thread) {
		return
	}
	h.mu.Lock()
	for _, run := range h.runs {
		if run.summary.ThreadID == thread && run.summary.Status == "running" {
			h.mu.Unlock()
			errJSON(w, http.StatusConflict, "a debate is running for this thread; wait for it to finish or cancel it")
			return
		}
	}
	h.mu.Unlock()
	code, out, errOut, err := h.runConfigCLI(nil, "--close-thread", thread, "--json")
	if err == nil && code == 0 {
		h.mu.Lock()
		h.setThreadClosedLocked(thread, true, "")
		h.mu.Unlock()
		h.closed.invalidate()
	}
	h.respondConfig(w, code, out, errOut, err)
}

// @MX:WARN: [AUTO] Commits a thread's executed file changes to a new branch of the hub's workspace clone and pushes it to the project's remote.
// @MX:REASON: The CLI verifies the exact file set, the base commit, the tracked branch and the project's test before the first git command, never forces the push, and takes a per-workspace lock; the route is blocked by GUARDIAN_UI_ACTIONS=off like running actions.
//
// publishThread runs `guardian.py --publish-thread` (docs/design/publish-thread.md). The optional JSON
// body {"branch": "..."} names the branch; the default is guardian/<thread>. Values reach the CLI only as argv.
func (h *debateHub) publishThread(w http.ResponseWriter, r *http.Request) {
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	if h.uiActionsBlocked(w, "publishing", "--publish-thread "+thread) {
		return
	}
	var req struct {
		Branch string `json:"branch"`
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, publishBodyLimit))
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "Invalid request body")
		return
	}
	if len(bytes.TrimSpace(body)) > 0 {
		if err := json.Unmarshal(body, &req); err != nil {
			errJSON(w, http.StatusBadRequest, "Invalid JSON body")
			return
		}
	}
	args := []string{"--publish-thread", thread, "--json", "--yes", "--actor", actorFor(r.RemoteAddr)}
	if req.Branch != "" {
		if !branchPattern.MatchString(req.Branch) {
			errJSON(w, http.StatusBadRequest, "invalid branch name")
			return
		}
		args = append(args, "--branch="+req.Branch)
	}
	code, out, errOut, err := h.runCLI(publishTimeout, nil, args...)
	if err == nil && code == 0 {
		h.published.invalidate() // a push may just have made the thread final
	}
	h.respondConfig(w, code, out, errOut, err)
}

// publishPlan is the read-only dry run of publishThread: `guardian.py --publish-thread T --plan --json`
// checks every precondition that comes before the test gate and returns {"ok":true,"plan":{...}}, or
// 409 with the reason. It changes nothing, so GUARDIAN_UI_ACTIONS=off does not block it. The optional
// query parameter branch is validated like the publish body's.
func (h *debateHub) publishPlan(w http.ResponseWriter, r *http.Request) {
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	args := []string{"--publish-thread", thread, "--plan", "--json"}
	if branch := r.URL.Query().Get("branch"); branch != "" {
		if !branchPattern.MatchString(branch) {
			errJSON(w, http.StatusBadRequest, "invalid branch name")
			return
		}
		args = append(args, "--branch="+branch)
	}
	code, out, errOut, err := h.runCLI(planTimeout, nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

// threadPublications lists a thread's publish attempts, newest first:
// `guardian.py --publications THREAD --json` -> {"publications": [{id, branch, commit_sha, remote, status, error, actor, created_at}]}.
func (h *debateHub) threadPublications(w http.ResponseWriter, r *http.Request) {
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--publications", thread, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// closedThreadList serves the cached CLOSED thread ids as {"threads": [...]}. Closing only hides a
// thread (docs/design/debate-teams-api.md); it never changes the thread's actions.
func (h *debateHub) closedThreadList(w http.ResponseWriter, r *http.Request) {
	closed, ok := h.closedThreads()
	if !ok {
		errJSON(w, http.StatusInternalServerError, "debate engine failed: cannot read the closed threads")
		return
	}
	threads := make([]string, 0, len(closed))
	for t := range closed {
		threads = append(threads, t)
	}
	sort.Strings(threads)
	jsonResponse(w, http.StatusOK, map[string][]string{"threads": threads})
}

// workspaceResetPlan is the read-only plan of a workspace reset:
// `guardian.py --workspace-reset PROJECT --plan --json` -> {"ok", "project_id", "workspace", "discard": [{path, state}],
// "keep": [{path, action_id, thread_id}]}.
func (h *debateHub) workspaceResetPlan(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !idPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	code, out, errOut, err := h.runCLI(planTimeout, nil, "--workspace-reset", id, "--plan", "--json")
	h.respondConfig(w, code, out, errOut, err)
}

// @MX:WARN: [AUTO] Discards uncommitted changes in a project's workspace clone on the hub.
// @MX:REASON: The CLI discards exactly the paths its plan lists (never the files of executed, unpublished actions, never a whole-tree clean), under the per-workspace lock, and only on the tracked branch; the route needs {"confirm": true} and is blocked by GUARDIAN_UI_ACTIONS=off.
//
// workspaceReset runs `guardian.py --workspace-reset PROJECT --yes --json`; the body {"confirm": true} is required.
// The optional "release": ["thread_..."] also gives up those threads' kept files (each becomes --release=THREAD);
// they can never be published afterwards. The answer is {"ok", "discarded": [paths], "released": [threads]}.
func (h *debateHub) workspaceReset(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !idPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	if h.uiActionsBlocked(w, "resetting a workspace", "--workspace-reset "+id) {
		return
	}
	var req struct {
		Confirm *bool    `json:"confirm"`
		Release []string `json:"release"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, resetBodyLimit)).Decode(&req); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	if req.Confirm == nil || !*req.Confirm {
		errJSON(w, http.StatusBadRequest, `confirm must be true: send {"confirm": true}`)
		return
	}
	if len(req.Release) > maxReleaseThreads {
		errJSON(w, http.StatusBadRequest, fmt.Sprintf("release must have at most %d threads", maxReleaseThreads))
		return
	}
	args := []string{"--workspace-reset", id, "--yes", "--json", "--actor", actorFor(r.RemoteAddr)}
	for _, thread := range req.Release {
		if !threadPattern.MatchString(thread) {
			errJSON(w, http.StatusBadRequest, "invalid thread in release")
			return
		}
		args = append(args, "--release="+thread) // single "=" entry: a value is never parsed as a flag
	}
	code, out, errOut, err := h.runCLI(h.cfg.actionTimeout(), nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

// setThreadClosedLocked sets thread_closed on the stored jobs of a thread, except the job named
// skip. Callers hold h.mu. Running jobs are left alone: their meta is rewritten when they finish.
func (h *debateHub) setThreadClosedLocked(thread string, closed bool, skip string) {
	if thread == "" {
		return
	}
	entries, _ := os.ReadDir(h.jobsDir())
	for _, e := range entries {
		if !e.IsDir() || !jobIDPattern.MatchString(e.Name()) || e.Name() == skip {
			continue
		}
		if _, live := h.runs[e.Name()]; live {
			continue
		}
		sum, err := h.readMeta(e.Name())
		if err != nil || sum.ThreadID != thread || sum.ThreadClosed == closed {
			continue
		}
		sum.ThreadClosed = closed
		h.writeMetaLocked(sum)
	}
}

// @MX:WARN: [AUTO] Executes judge-proposed actions, including shell commands for server_remediation, on the hub after a person confirms in the dashboard.
// @MX:REASON: Guarded by the confirm_id echo, GUARDIAN_UI_ACTIONS=off, a per-id in-process lock, the CLI's PROPOSED->RUNNING DB transition and pre-run revalidation; the child runs on a context detached from the request so a client disconnect cannot leave a half-run action.
func (h *debateHub) runAction(w http.ResponseWriter, r *http.Request) {
	id, ok := actionIDParam(w, r)
	if !ok {
		return
	}
	if h.uiActionsBlocked(w, "running actions", "--run-action "+strconv.Itoa(id)) {
		return
	}
	var req struct {
		ConfirmID *int64 `json:"confirm_id"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, actionBodyLimit)).Decode(&req); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	if req.ConfirmID == nil || *req.ConfirmID != int64(id) {
		errJSON(w, http.StatusBadRequest, "confirm_id must match the action id")
		return
	}

	h.mu.Lock()
	if _, busy := h.actionRuns[id]; busy {
		h.mu.Unlock()
		errJSON(w, http.StatusConflict, "action is already running")
		return
	}
	h.actionRuns[id] = struct{}{}
	h.mu.Unlock()
	defer func() {
		h.mu.Lock()
		delete(h.actionRuns, id)
		h.mu.Unlock()
	}()

	code, out, errOut, err := h.runCLI(h.cfg.actionTimeout(), nil,
		"--run-action", strconv.Itoa(id), "--yes", "--json", "--actor", actorFor(r.RemoteAddr))
	h.respondConfig(w, code, out, errOut, err)
}

// failStaleAction moves a RUNNING action to FAILED (`guardian.py --fail-stale-action ID --json`) when
// its process is gone (e.g. the hub restarted mid-run). The body {"confirm_id": <id>} must echo the id.
// It is refused with 409 while this hub process is still running the action; CLI exit 4 (not RUNNING)
// and 3 (unknown) map through respondConfig. Blocked by GUARDIAN_UI_ACTIONS=off.
func (h *debateHub) failStaleAction(w http.ResponseWriter, r *http.Request) {
	id, ok := actionIDParam(w, r)
	if !ok {
		return
	}
	if h.uiActionsBlocked(w, "marking an action failed", "--fail-stale-action "+strconv.Itoa(id)) {
		return
	}
	var req struct {
		ConfirmID *int64 `json:"confirm_id"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, actionBodyLimit)).Decode(&req); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	if req.ConfirmID == nil || *req.ConfirmID != int64(id) {
		errJSON(w, http.StatusBadRequest, "confirm_id must match the action id")
		return
	}
	h.mu.Lock()
	_, live := h.actionRuns[id]
	h.mu.Unlock()
	if live {
		errJSON(w, http.StatusConflict, "this hub is still running the action; wait for it to finish")
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--fail-stale-action", strconv.Itoa(id), "--json", "--actor", actorFor(r.RemoteAddr))
	h.respondConfig(w, code, out, errOut, err)
}

// ---- retrieval totals ----

// maxResultScanBytes bounds how much of a result.json the list endpoint is willing to scan.
const maxResultScanBytes = 32 << 20

// debateRetrieval is the per-run retrieval total the engine writes into result.json.
type debateRetrieval struct {
	Results int64 `json:"results"`
	Bytes   int64 `json:"bytes"`
}

// readRetrieval returns the top-level "retrieval" totals of a job's result.json, or nil when the file
// is missing, oversized, malformed or has no usable retrieval object. It streams the top-level keys
// and decodes only that one value.
func (h *debateHub) readRetrieval(id string) *debateRetrieval {
	f, err := os.Open(filepath.Join(h.jobsDir(), id, "result.json"))
	if err != nil {
		return nil
	}
	defer f.Close()
	if st, err := f.Stat(); err != nil || !st.Mode().IsRegular() || st.Size() > maxResultScanBytes {
		return nil
	}
	dec := json.NewDecoder(f)
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return nil
	}
	for dec.More() {
		keyTok, err := dec.Token()
		if err != nil {
			return nil
		}
		key, _ := keyTok.(string)
		if key == "retrieval" {
			var out struct {
				Results *int64 `json:"results"`
				Bytes   *int64 `json:"bytes"`
			}
			if err := dec.Decode(&out); err != nil || out.Results == nil || out.Bytes == nil {
				return nil
			}
			return &debateRetrieval{Results: *out.Results, Bytes: *out.Bytes}
		}
		var skip json.RawMessage
		if err := dec.Decode(&skip); err != nil {
			return nil
		}
	}
	return nil
}

// sortedPairs turns a map into "<prefix>KEY=VALUE" argv entries in key order: one entry each, so a value can never
// be parsed as a separate flag.
func sortedPairs(prefix string, values map[string]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]string, 0, len(keys))
	for _, key := range keys {
		out = append(out, prefix+key+"="+values[key])
	}
	return out
}

package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"regexp"
	"time"
)

// Plan routes (docs/design/fleet-provisioning.md, P3). A plan is a set of action rows with a plan_id; the
// Python CLI owns validation, scheduling and the per-step safety checks. This file only validates ids,
// spawns the CLI without a shell and keeps one in-flight run per plan.

const (
	// planRunTimeout bounds one plan run: up to 100 steps, each an install or removal.
	planRunTimeout      = 4 * time.Hour
	defaultPlanGrace    = 3 * time.Second
	maxPlanIDChars      = 105
	planConfirmBodySize = actionBodyLimit
)

var planIDPattern = regexp.MustCompile(`^plan_[A-Za-z0-9_]{1,100}$`)

func (c debateConfig) planStartGrace() time.Duration {
	if c.PlanStartGrace > 0 {
		return c.PlanStartGrace
	}
	return defaultPlanGrace
}

func planIDParam(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("id")
	if len(id) > maxPlanIDChars || !planIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid plan id")
		return "", false
	}
	return id, true
}

func (h *debateHub) registerPlanRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/plans", h.guard(h.listPlans))
	mux.HandleFunc("GET /api/plans/{id}", h.guard(h.showPlan))
	mux.HandleFunc("POST /api/plans/{id}/run", h.guard(h.runPlan))
	mux.HandleFunc("POST /api/plans/{id}/reject", h.guard(h.rejectPlan))
}

func (h *debateHub) listPlans(w http.ResponseWriter, r *http.Request) {
	args := []string{"--plans"}
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

func (h *debateHub) showPlan(w http.ResponseWriter, r *http.Request) {
	id, ok := planIDParam(w, r)
	if !ok {
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--plan-show", id, "--json")
	h.respondConfig(w, code, out, errOut, err)
}

func (h *debateHub) rejectPlan(w http.ResponseWriter, r *http.Request) {
	id, ok := planIDParam(w, r)
	if !ok {
		return
	}
	if h.uiActionsBlocked(w, "rejecting plans", "--plan-reject "+id) {
		return
	}
	code, out, errOut, err := h.runConfigCLI(nil, "--plan-reject", id, "--json", "--actor", actorFor(r.RemoteAddr))
	h.respondConfig(w, code, out, errOut, err)
}

type planRunResult struct {
	code   int
	stdout []byte
	stderr string
	err    error
}

// @MX:WARN: [AUTO] Runs every step of a plan (package installs and removals on fleet hosts) after a person confirms in the dashboard.
// @MX:REASON: Guarded by the confirm_id echo, GUARDIAN_UI_ACTIONS=off, a per-plan in-process lock and the CLI's own plan lock, per-host lock, PROPOSED->RUNNING transitions and drift checks; the child runs on a context detached from the request, so a client disconnect cannot stop a plan half way.
//
// A plan can run for a long time, so the run is asynchronous: the handler waits for the CLI only for
// the start grace, which is enough to see it refuse the plan (exit 2/3/4). A run that is still going
// after that is answered 202 and the dashboard polls GET /api/plans/{id}; the result is in the DB.
func (h *debateHub) runPlan(w http.ResponseWriter, r *http.Request) {
	id, ok := planIDParam(w, r)
	if !ok {
		return
	}
	if h.uiActionsBlocked(w, "running plans", "--plan-run "+id) {
		return
	}
	var req struct {
		ConfirmID *string `json:"confirm_id"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, planConfirmBodySize)).Decode(&req); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	if req.ConfirmID == nil || *req.ConfirmID != id {
		errJSON(w, http.StatusBadRequest, "confirm_id must match the plan id")
		return
	}

	h.mu.Lock()
	if _, busy := h.planRuns[id]; busy {
		h.mu.Unlock()
		errJSON(w, http.StatusConflict, "plan is already running")
		return
	}
	h.planRuns[id] = struct{}{}
	h.mu.Unlock()

	done := make(chan planRunResult, 1)
	actor := actorFor(r.RemoteAddr)
	go func() {
		code, out, errOut, err := h.runCLI(planRunTimeout, nil, "--plan-run", id, "--yes", "--json", "--actor", actor)
		h.mu.Lock()
		delete(h.planRuns, id)
		h.mu.Unlock()
		done <- planRunResult{code, out, errOut, err}
	}()

	timer := time.NewTimer(h.cfg.planStartGrace())
	defer timer.Stop()
	select {
	case res := <-done:
		h.respondConfig(w, res.code, res.stdout, res.stderr, res.err)
	case <-timer.C:
		jsonResponse(w, http.StatusAccepted, map[string]interface{}{"started": true, "plan_id": id})
	}
}

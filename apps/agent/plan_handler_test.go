package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPlansListPassthrough(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/plans", "")
	if rec.Code != 200 || decode(t, rec)["plans"] == nil {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--plans --json" {
		t.Errorf("argv without filters: %q", got)
	}
	rec = th.call("GET", "/api/plans?thread=thread_abc123&project=fleet", "")
	if rec.Code != 200 {
		t.Fatalf("list filtered: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--plans thread_abc123 --project=fleet --json" {
		t.Errorf("argv with filters: %q", got)
	}
	for _, bad := range []string{"thread=nope", "thread=thread_a;rm", "project=Bad%20Id", "project=-x"} {
		if rec := th.call("GET", "/api/plans?"+bad, ""); rec.Code != 400 {
			t.Errorf("%s: status %d, want 400", bad, rec.Code)
		}
	}
}

func TestPlanBadIDs(t *testing.T) {
	th := newTestHub(t, nil)
	long := "plan_" + strings.Repeat("a", 101)
	for _, id := range []string{"1", "plan_", "plan-a", "thread_a", "plan_a%20b", "plan_a;rm", long, "Plan_a"} {
		for _, rt := range []struct{ method, path, body string }{
			{"GET", "/api/plans/" + id, ""},
			{"POST", "/api/plans/" + id + "/run", `{"confirm_id":"` + id + `"}`},
			{"POST", "/api/plans/" + id + "/reject", ""},
		} {
			if rec := th.call(rt.method, rt.path, rt.body); rec.Code != 400 {
				t.Errorf("%s %s: status %d, want 400", rt.method, rt.path, rec.Code)
			}
		}
	}
}

func TestPlanShowAndNotFound(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/plans/plan_a_r1", "")
	if rec.Code != 200 {
		t.Fatalf("show: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(th.lastArgs(t), " "); got != "--plan-show plan_a_r1 --json" {
		t.Errorf("argv: %q", got)
	}
	rec = th.call("GET", "/api/plans/plan_missing", "")
	if rec.Code != 404 || decode(t, rec)["error"] != "plan plan_missing not found" {
		t.Errorf("exit 3: %d %s", rec.Code, rec.Body.String())
	}
}

func TestPlanRunQuickSuccess(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/plans/plan_a_r1/run", `{"confirm_id":"plan_a_r1"}`)
	if rec.Code != 200 || decode(t, rec)["outcome"] != "EXECUTED" {
		t.Fatalf("run: %d %s", rec.Code, rec.Body.String())
	}
	joined := strings.Join(th.lastArgs(t), " ")
	for _, want := range []string{"--plan-run plan_a_r1", "--yes", "--json", "--actor dashboard@192.0.2.1"} {
		if !strings.Contains(joined, want) {
			t.Errorf("argv %q misses %q", joined, want)
		}
	}
}

func TestPlanRunConfirmMismatch(t *testing.T) {
	th := newTestHub(t, nil)
	for _, body := range []string{`{"confirm_id":"plan_other"}`, `{}`, `{"confirm_id":12}`, `not json`, ``} {
		if rec := th.call("POST", "/api/plans/plan_a_r1/run", body); rec.Code != 400 {
			t.Errorf("body %q: status %d, want 400", body, rec.Code)
		}
	}
	rec := th.call("POST", "/api/plans/plan_a_r1/run", `{"confirm_id":"plan_other"}`)
	if decode(t, rec)["error"] != "confirm_id must match the plan id" {
		t.Errorf("error text: %s", rec.Body.String())
	}
	big := `{"confirm_id":"plan_a_r1","pad":"` + strings.Repeat("x", 5000) + `"}`
	if rec := th.call("POST", "/api/plans/plan_a_r1/run", big); rec.Code != 413 {
		t.Errorf("oversized body: %d", rec.Code)
	}
	if _, err := os.Stat(filepath.Join(th.dir, "planran-plan_a_r1.marker")); err == nil {
		t.Error("CLI ran despite a confirm mismatch")
	}
}

func TestPlanRunDisabledStillAllowsRead(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.UIActions = "off" })
	rec := th.call("POST", "/api/plans/plan_a_r1/run", `{"confirm_id":"plan_a_r1"}`)
	if rec.Code != 403 || !strings.Contains(decode(t, rec)["error"].(string), "GUARDIAN_UI_ACTIONS=off") {
		t.Errorf("run: %d %s", rec.Code, rec.Body.String())
	}
	if rec := th.call("POST", "/api/plans/plan_a_r1/reject", ""); rec.Code != 403 {
		t.Errorf("reject: %d", rec.Code)
	}
	if rec := th.call("GET", "/api/plans/plan_a_r1", ""); rec.Code != 200 {
		t.Errorf("show: %d", rec.Code)
	}
	if _, err := os.Stat(filepath.Join(th.dir, "planran-plan_a_r1.marker")); err == nil {
		t.Error("CLI ran while disabled")
	}
}

func TestPlanRejectArgv(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/plans/plan_a_r1/reject", "")
	if rec.Code != 200 {
		t.Fatalf("reject: %d %s", rec.Code, rec.Body.String())
	}
	if joined := strings.Join(th.lastArgs(t), " "); !strings.HasPrefix(joined, "--plan-reject plan_a_r1 --json --actor dashboard@192.0.2.1") {
		t.Errorf("argv: %q", joined)
	}
}

func TestPlanRunConflictExit4(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("POST", "/api/plans/plan_done/run", `{"confirm_id":"plan_done"}`)
	if rec.Code != 409 || decode(t, rec)["error"] != "plan plan_done has nothing left to run" {
		t.Errorf("exit 4: %d %s", rec.Code, rec.Body.String())
	}
	th.hub.mu.Lock()
	_, busy := th.hub.planRuns["plan_done"]
	th.hub.mu.Unlock()
	if busy {
		t.Error("a refused plan stays registered as running")
	}
}

func TestPlanRunLongRunAnswers202ThenDuplicateConflicts(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.PlanStartGrace = 200 * time.Millisecond })
	body := `{"confirm_id":"plan_slow1"}`
	rec := th.call("POST", "/api/plans/plan_slow1/run", body)
	m := decode(t, rec)
	if rec.Code != 202 || m["started"] != true || m["plan_id"] != "plan_slow1" {
		t.Fatalf("long run: %d %s", rec.Code, rec.Body.String())
	}
	rec = th.call("POST", "/api/plans/plan_slow1/run", body)
	if rec.Code != 409 || decode(t, rec)["error"] != "plan is already running" {
		t.Errorf("duplicate: %d %s", rec.Code, rec.Body.String())
	}
	// another plan is independent
	if rec := th.call("POST", "/api/plans/plan_other/run", `{"confirm_id":"plan_other"}`); rec.Code != 200 {
		t.Errorf("other plan: %d %s", rec.Code, rec.Body.String())
	}
	// the run goes on without the request and releases the id when it ends
	marker := filepath.Join(th.dir, "planran-plan_slow1.marker")
	deadline := time.Now().Add(10 * time.Second)
	for {
		th.hub.mu.Lock()
		_, busy := th.hub.planRuns["plan_slow1"]
		th.hub.mu.Unlock()
		_, err := os.Stat(marker)
		if !busy && err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("run never finished (busy=%v, marker err=%v)", busy, err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if rec := th.call("POST", "/api/plans/plan_slow1/run", body); rec.Code != 202 && rec.Code != 200 {
		t.Errorf("rerun after finish: %d %s", rec.Code, rec.Body.String())
	}
}

package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestMain keeps exec-route tests from writing the audit log under the real ~/.guardian/data.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "guardian-audit-test")
	if err != nil {
		panic(err)
	}
	os.Setenv("GUARDIAN_DATA_DIR", dir)
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

func auditReq() *http.Request {
	r := httptest.NewRequest("POST", "/api/exec", nil)
	r.RemoteAddr = "100.64.0.9:5555"
	return r
}

func TestAuditAppendAndRead(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("GUARDIAN_DATA_DIR", dir)
	long := strings.Repeat("x", 5000)
	recordExec(auditReq(), "/api/exec", []string{"local"}, "echo hi", map[string]int{"local": 0}, time.Now())
	recordExec(auditReq(), "/api/fleet/exec", []string{"a", "b"}, long, map[string]int{"a": 0, "b": 1}, time.Now())

	st, err := os.Stat(filepath.Join(dir, "audit", "exec.jsonl"))
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("file: %v %v", err, st)
	}
	if d, _ := os.Stat(filepath.Join(dir, "audit")); d.Mode().Perm() != 0o700 {
		t.Fatalf("dir mode %v", d.Mode())
	}

	rec := httptest.NewRecorder()
	handleAudit(rec, httptest.NewRequest("GET", "/api/audit", nil))
	var body struct{ Entries []auditEntry }
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || len(body.Entries) != 2 {
		t.Fatalf("%v %s", err, rec.Body)
	}
	newest, oldest := body.Entries[0], body.Entries[1]
	if newest.Route != "/api/fleet/exec" || newest.Status != "error" || len(newest.Command) != 4096 || newest.Actor != "dashboard@100.64.0.9" {
		t.Fatalf("newest: %+v", newest)
	}
	if oldest.Status != "ok" || oldest.Targets[0] != "local" || oldest.ExitCodes["local"] != 0 {
		t.Fatalf("oldest: %+v", oldest)
	}
	if _, err := time.Parse(time.RFC3339, oldest.TS); err != nil {
		t.Fatal(err)
	}
}

func TestAuditLimitAndTail(t *testing.T) {
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	var wg sync.WaitGroup
	for i := 0; i < 300; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			recordExec(auditReq(), "/api/exec", []string{"local"}, strings.Repeat("c", 600), map[string]int{"local": 0}, time.Now())
		}()
	}
	wg.Wait()
	get := func(q string) []auditEntry {
		rec := httptest.NewRecorder()
		handleAudit(rec, httptest.NewRequest("GET", "/api/audit"+q, nil))
		var b struct{ Entries []auditEntry }
		if rec.Code != 200 || json.Unmarshal(rec.Body.Bytes(), &b) != nil {
			t.Fatalf("%d %s", rec.Code, rec.Body)
		}
		return b.Entries
	}
	if n := len(get("")); n != 200 {
		t.Fatalf("default limit gave %d", n)
	}
	if n := len(get("?limit=5")); n != 5 {
		t.Fatalf("limit=5 gave %d", n)
	}
	if n := len(get("?limit=99999")); n != 300 {
		t.Fatalf("max clamp gave %d", n)
	}
	rec := httptest.NewRecorder()
	handleAudit(rec, httptest.NewRequest("GET", "/api/audit?limit=abc", nil))
	if rec.Code != 400 {
		t.Fatalf("bad limit: %d", rec.Code)
	}
}

func TestAuditEmptyAndWriteFailure(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("GUARDIAN_DATA_DIR", dir)
	rec := httptest.NewRecorder()
	handleAudit(rec, httptest.NewRequest("GET", "/api/audit", nil))
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"entries":[]`) {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	// A data dir that is a file makes every write fail; recordExec must not panic.
	bad := filepath.Join(dir, "afile")
	_ = os.WriteFile(bad, nil, 0o600)
	t.Setenv("GUARDIAN_DATA_DIR", bad)
	recordExec(auditReq(), "/api/exec", nil, "x", nil, time.Now())
}

func TestExecRouteIsAudited(t *testing.T) {
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	// Same code path as the /api/exec handler: executeRaw then recordExec.
	start := time.Now()
	code, _, _, _ := executeRaw("true", 5)
	recordExec(auditReq(), "/api/exec", []string{"local"}, "true", map[string]int{"local": code}, start)
	entries, err := readAuditTail(10)
	if err != nil || len(entries) != 1 || entries[0].Status != "ok" {
		t.Fatalf("%v %+v", err, entries)
	}
}

func TestAuditRotationAndReadAcrossFiles(t *testing.T) {
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	old := auditMaxBytes
	auditMaxBytes = 600
	defer func() { auditMaxBytes = old }()
	for i := 0; i < 40; i++ {
		recordExec(auditReq(), "/api/exec", []string{"local"}, "cmd-"+strings.Repeat("x", 30)+string(rune('A'+i%26)), map[string]int{"local": i}, time.Now())
	}
	p := auditFilePath()
	if _, err := os.Stat(p + ".1"); err != nil {
		t.Fatalf("no rotated file: %v", err)
	}
	if _, err := os.Stat(p + ".6"); err == nil {
		t.Fatal("more than auditKeepFiles rotated files")
	}
	entries, err := readAuditTail(1000)
	if err != nil || len(entries) < 10 {
		t.Fatalf("entries %d err %v", len(entries), err)
	}
	for i := 1; i < len(entries); i++ {
		if entries[i-1].ExitCodes["local"] < entries[i].ExitCodes["local"] {
			t.Fatalf("not newest first at %d", i)
		}
	}
	if entries[0].ExitCodes["local"] != 39 {
		t.Fatalf("newest = %+v", entries[0])
	}
	// A small limit that spans the current file boundary still returns exactly limit entries.
	got, _ := readAuditTail(12)
	if len(got) != 12 || got[11].ExitCodes["local"] != 28 {
		t.Fatalf("limit across files: %d %+v", len(got), got)
	}
}

func TestAuditActorKinds(t *testing.T) {
	r := auditReq()
	if got := auditActor(r); got != "dashboard@100.64.0.9" {
		t.Errorf("none: %s", got)
	}
	r.AddCookie(&http.Cookie{Name: sessionCookie, Value: "v2.x"})
	if got := auditActor(r); got != "session@100.64.0.9" {
		t.Errorf("cookie: %s", got)
	}
	r.Header.Set("Authorization", "Bearer abc")
	if got := auditActor(r); got != "token@100.64.0.9" {
		t.Errorf("bearer: %s", got)
	}
}

func TestExecRejectionsAreAudited(t *testing.T) {
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/fleet/exec/stream", strings.NewReader(`{"command":""}`))
	req.RemoteAddr = "100.64.0.9:1"
	handleFleetExecStream(rec, req)
	if rec.Code != 400 {
		t.Fatalf("code %d", rec.Code)
	}
	entries, _ := readAuditTail(5)
	if len(entries) != 1 || entries[0].Status != "rejected" || entries[0].Detail == "" || entries[0].Route != "/api/fleet/exec/stream" {
		t.Fatalf("entries: %+v", entries)
	}
}

func TestHubGuardWhenHubSudoOff(t *testing.T) {
	all := []FleetNode{{ID: "hub-1", Name: "hub", GuardianRole: "hub"}, {ID: "n1", Name: "n1"}}
	t.Setenv("GUARDIAN_HUB_SUDO", "on")
	if got, err := execNodes(all, []string{"hub-1"}); err != nil || len(got) != 1 {
		t.Fatalf("sudo on: %v %v", got, err)
	}
	t.Setenv("GUARDIAN_HUB_SUDO", "off")
	for _, tgt := range []string{"hub-1", "hub"} {
		if _, err := execNodes(all, []string{tgt}); !errors.Is(err, errHubNotTarget) || execErrorStatus(err) != 409 {
			t.Errorf("target %s: %v", tgt, err)
		}
	}
	if got, err := execNodes(all, nil); err != nil || len(got) != 1 || got[0].ID != "n1" {
		t.Fatalf("implicit fleet should leave the hub out: %v %v", got, err)
	}
	if !hubIDIn(all, "hub-1") || hubIDIn(all, "n1") {
		t.Fatal("hubIDIn")
	}
}

func TestHubSudoObservedFromProcStatus(t *testing.T) {
	t.Setenv("GUARDIAN_HUB_SUDO", "")
	old := readProcStatus
	defer func() { readProcStatus = old }()
	readProcStatus = func() ([]byte, error) { return []byte("Name:\tagent\nNoNewPrivs:\t1\n"), nil }
	if !hubSudoOff() {
		t.Error("NoNewPrivs 1 means hub sudo off")
	}
	readProcStatus = func() ([]byte, error) { return []byte("NoNewPrivs:\t0\n"), nil }
	if hubSudoOff() {
		t.Error("NoNewPrivs 0 means hub sudo on")
	}
	readProcStatus = func() ([]byte, error) { return nil, os.ErrNotExist }
	if hubSudoOff() {
		t.Error("no /proc must not refuse")
	}
}

func TestAuditRecordsTheEngineOrigin(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("GUARDIAN_DATA_DIR", dir)
	labelled := func(source, op, project string) *http.Request {
		r := auditReq()
		r.Header.Set("X-Guardian-Source", source)
		r.Header.Set("X-Guardian-Op", op)
		r.Header.Set("X-Guardian-Project", project)
		return r
	}
	recordExec(labelled("ops_check", "disk_usage", "fleet"), "/api/exec/remote", []string{"n1"}, "df", map[string]int{"n1": 0}, time.Now())
	withAction := labelled("action", "apt_clean", "fleet")
	withAction.Header.Set("X-Guardian-Action", "75")
	recordExec(withAction, "/api/exec/remote", []string{"n1"}, "apt-get clean", map[string]int{"n1": 0}, time.Now())
	badAction := labelled("action", "apt_clean", "fleet")
	badAction.Header.Set("X-Guardian-Action", "75; rm")
	recordExec(badAction, "/api/exec/remote", []string{"n1"}, "apt-get clean", map[string]int{"n1": 0}, time.Now())
	recordExec(labelled("other", "x", "y"), "/api/exec", []string{"local"}, "id", map[string]int{"local": 0}, time.Now())
	recordExec(labelled("ops_check", "bad op\n{", "fleet"), "/api/exec", []string{"local"}, "id", map[string]int{"local": 0}, time.Now())
	recordExec(auditReq(), "/api/exec", []string{"local"}, "id", map[string]int{"local": 0}, time.Now())

	rec := httptest.NewRecorder()
	handleAudit(rec, httptest.NewRequest("GET", "/api/audit", nil))
	var body struct{ Entries []auditEntry }
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || len(body.Entries) != 6 {
		t.Fatalf("%v %s", err, rec.Body)
	}
	got := [][4]string{}
	for i := len(body.Entries) - 1; i >= 0; i-- {
		e := body.Entries[i]
		got = append(got, [4]string{e.Source, e.Op, e.Project, e.ActionID})
	}
	want := [][4]string{{"ops_check", "disk_usage", "fleet", ""}, {"action", "apt_clean", "fleet", "75"},
		{"action", "apt_clean", "fleet", ""}, {"", "", "", ""}, {"ops_check", "", "fleet", ""}, {"", "", "", ""}}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("entry %d: got %v, want %v", i, got[i], want[i])
		}
	}
	if strings.Contains(rec.Body.String(), `"source":""`) {
		t.Fatalf("empty labels must be omitted: %s", rec.Body)
	}
}

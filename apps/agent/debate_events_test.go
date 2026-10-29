package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const eventsTestJob = "dbt_20261008_120000_abc123"

// seedEventsJob writes a meta.json (status given) and, when events is non-nil, events.jsonl.
func (th *testHub) seedEventsJob(t *testing.T, id, status string, events *string) string {
	t.Helper()
	dir := filepath.Join(th.dir, "data", "debate-jobs", id)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	th.setStatus(t, id, status)
	if events != nil {
		if err := os.WriteFile(filepath.Join(dir, "events.jsonl"), []byte(*events), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func (th *testHub) setStatus(t *testing.T, id, status string) {
	t.Helper()
	meta := fmt.Sprintf(`{"id":%q,"status":%q,"started_at":"2026-10-08T12:00:00Z"}`, id, status)
	if err := os.WriteFile(filepath.Join(th.dir, "data", "debate-jobs", id, "meta.json"), []byte(meta), 0o600); err != nil {
		t.Fatal(err)
	}
}

func evLine(seq int, typ string) string {
	return fmt.Sprintf(`{"seq":%d,"type":%q}`+"\n", seq, typ)
}

type eventsResp struct {
	Events []map[string]interface{} `json:"events"`
	Next   float64                  `json:"next"`
	Done   bool                     `json:"done"`
}

func (th *testHub) pollEvents(t *testing.T, id, query string) eventsResp {
	t.Helper()
	rec := th.call("GET", "/api/debates/"+id+"/events"+query, "")
	if rec.Code != 200 {
		t.Fatalf("events%s: %d %s", query, rec.Code, rec.Body.String())
	}
	var out eventsResp
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("bad body %q: %v", rec.Body.String(), err)
	}
	if out.Events == nil {
		t.Fatalf("events must be an array: %s", rec.Body.String())
	}
	return out
}

func TestDebateEventsPollAndAfter(t *testing.T) {
	th := newTestHub(t, nil)
	body := evLine(1, "debate_started") + evLine(2, "round_started") + evLine(3, "agent_started")
	th.seedEventsJob(t, eventsTestJob, "running", &body)

	all := th.pollEvents(t, eventsTestJob, "")
	if len(all.Events) != 3 || all.Next != 3 || all.Done {
		t.Fatalf("all = %+v", all)
	}
	part := th.pollEvents(t, eventsTestJob, "?after=2")
	if len(part.Events) != 1 || part.Events[0]["type"] != "agent_started" || part.Next != 3 {
		t.Fatalf("after=2 = %+v", part)
	}
	none := th.pollEvents(t, eventsTestJob, "?after=3")
	if len(none.Events) != 0 || none.Next != 3 || none.Done {
		t.Fatalf("after=3 = %+v", none)
	}
	th.setStatus(t, eventsTestJob, "completed")
	if done := th.pollEvents(t, eventsTestJob, "?after=3"); !done.Done {
		t.Fatalf("finished job should be done: %+v", done)
	}
}

func TestDebateEventsLimit(t *testing.T) {
	th := newTestHub(t, nil)
	var sb strings.Builder
	for i := 1; i <= 1005; i++ {
		sb.WriteString(evLine(i, "round_started"))
	}
	body := sb.String()
	th.seedEventsJob(t, eventsTestJob, "completed", &body)
	first := th.pollEvents(t, eventsTestJob, "")
	if len(first.Events) != 1000 || first.Next != 1000 || first.Done {
		t.Fatalf("first page: len=%d next=%v done=%v", len(first.Events), first.Next, first.Done)
	}
	second := th.pollEvents(t, eventsTestJob, "?after=1000")
	if len(second.Events) != 5 || second.Next != 1005 || !second.Done {
		t.Fatalf("second page: len=%d next=%v done=%v", len(second.Events), second.Next, second.Done)
	}
}

func TestDebateEventsSkipsBrokenOversizedAndPartialLines(t *testing.T) {
	th := newTestHub(t, nil)
	big := `{"seq":2,"type":"big","pad":"` + strings.Repeat("x", maxEventLine) + `"}` + "\n"
	body := evLine(1, "a") + "not json\n" + big + `[1,2]` + "\n" + `{"type":"noseq"}` + "\n" + evLine(3, "b") + `{"seq":4,"type":"par`
	dir := th.seedEventsJob(t, eventsTestJob, "running", &body)

	got := th.pollEvents(t, eventsTestJob, "")
	if len(got.Events) != 2 || got.Events[0]["type"] != "a" || got.Events[1]["type"] != "b" || got.Next != 3 {
		t.Fatalf("got %+v", got)
	}
	// The writer finishes the partial line: it becomes visible on the next read.
	f, err := os.OpenFile(filepath.Join(dir, "events.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = f.WriteString(`tial"}` + "\n")
	f.Close()
	got = th.pollEvents(t, eventsTestJob, "?after=3")
	if len(got.Events) != 1 || got.Events[0]["type"] != "partial" || got.Next != 4 {
		t.Fatalf("after completing line: %+v", got)
	}
}

func TestDebateEventsMissingFile(t *testing.T) {
	th := newTestHub(t, nil)
	th.seedEventsJob(t, eventsTestJob, "completed", nil)
	got := th.pollEvents(t, eventsTestJob, "?after=5")
	if len(got.Events) != 0 || got.Next != 5 || !got.Done {
		t.Fatalf("got %+v", got)
	}
	rec := th.call("GET", "/api/debates/"+eventsTestJob+"/events?follow=1", "")
	if rec.Code != 200 || strings.TrimSpace(rec.Body.String()) != `{"type":"end","status":"completed"}` {
		t.Fatalf("follow on finished job without file: %d %q", rec.Code, rec.Body.String())
	}
}

func TestDebateEventsRejectsBadInput(t *testing.T) {
	th := newTestHub(t, nil)
	th.seedEventsJob(t, eventsTestJob, "completed", nil)
	for target, want := range map[string]int{
		"/api/debates/not-a-job/events":                                        400,
		"/api/debates/..%2F..%2Fetc/events":                                    400,
		"/api/debates/dbt_20200101_000000_aaaaaa/events":                       404,
		"/api/debates/" + eventsTestJob + "/events?after=-1":                   400,
		"/api/debates/" + eventsTestJob + "/events?after=abc":                  400,
		"/api/debates/" + eventsTestJob + "/events?after=1.5":                  400,
		"/api/debates/" + eventsTestJob + "/events?after=%2B1":                 400,
		"/api/debates/" + eventsTestJob + "/events?follow=maybe":               400,
		"/api/debates/" + eventsTestJob + "/events?after=99999999999999999999": 400,
	} {
		if rec := th.call("GET", target, ""); rec.Code != want {
			t.Errorf("%s: %d, want %d", target, rec.Code, want)
		}
	}
}

func TestDebateEventsSymlinkNotFollowed(t *testing.T) {
	th := newTestHub(t, nil)
	dir := th.seedEventsJob(t, eventsTestJob, "completed", nil)
	outside := filepath.Join(th.dir, "outside.jsonl")
	if err := os.WriteFile(outside, []byte(evLine(1, "leak")), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "events.jsonl")); err != nil {
		t.Fatal(err)
	}
	rec := th.call("GET", "/api/debates/"+eventsTestJob+"/events", "")
	if strings.Contains(rec.Body.String(), "leak") {
		t.Fatalf("symlinked events file was followed: %s", rec.Body.String())
	}
	list := decode(t, th.call("GET", "/api/debates", ""))["debates"].([]interface{})
	if list[0].(map[string]interface{})["events"] != false {
		t.Errorf("symlink must not count as an events file: %v", list[0])
	}
}

func TestDebateEventsRequireAuth(t *testing.T) {
	th := newTestHub(t, nil)
	th.seedEventsJob(t, eventsTestJob, "completed", nil)
	h := withAuth(th.h, testToken)
	path := "/api/debates/" + eventsTestJob + "/events"
	if rec := do(h, "GET", path, "", nil); rec.Code != http.StatusUnauthorized {
		t.Errorf("without token: %d", rec.Code)
	}
	if rec := do(h, "GET", path+"?follow=1", "", nil); rec.Code != http.StatusUnauthorized {
		t.Errorf("follow without token: %d", rec.Code)
	}
	rec := do(h, "GET", path, "", func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+testToken) })
	if rec.Code != 200 {
		t.Errorf("with token: %d %s", rec.Code, rec.Body.String())
	}
}

func TestDebateEventsWorkWithoutCLI(t *testing.T) {
	// Events are read from the job directory, so a missing CLI does not hide a run's history.
	th := newTestHub(t, func(c *debateConfig) { c.CLI = "/nonexistent/guardian.py" })
	body := evLine(1, "debate_started")
	th.seedEventsJob(t, eventsTestJob, "completed", &body)
	rec := th.call("GET", "/api/debates/"+eventsTestJob+"/events", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("code %d %s", rec.Code, rec.Body.String())
	}
	if events, _ := decode(t, rec)["events"].([]interface{}); len(events) != 1 {
		t.Errorf("events %v", events)
	}
}

func TestListDebatesReportsEventsFlag(t *testing.T) {
	th := newTestHub(t, nil)
	body := evLine(1, "debate_started")
	th.seedEventsJob(t, "dbt_20261008_120000_aaaaaa", "completed", &body)
	th.seedEventsJob(t, "dbt_20261008_120001_bbbbbb", "completed", nil)
	flags := map[string]interface{}{}
	for _, it := range decode(t, th.call("GET", "/api/debates", ""))["debates"].([]interface{}) {
		m := it.(map[string]interface{})
		flags[m["id"].(string)] = m["events"]
	}
	if flags["dbt_20261008_120000_aaaaaa"] != true || flags["dbt_20261008_120001_bbbbbb"] != false {
		t.Errorf("flags = %v", flags)
	}
}

func TestDebateLaunchPassesEventsFile(t *testing.T) {
	th := newTestHub(t, nil)
	id := th.startJob(t, "events argv")["id"].(string)
	th.waitDone(t, id)
	data, err := os.ReadFile(filepath.Join(th.dir, "debate-args.txt"))
	if err != nil {
		t.Fatal(err)
	}
	want := "--events-file=" + filepath.Join(th.dir, "data", "debate-jobs", id, "events.jsonl")
	found := false
	for _, a := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if a == want {
			found = true
		}
		if a == "--events-file" {
			t.Errorf("flag and value passed as separate argv entries")
		}
	}
	if !found {
		t.Errorf("argv lacks %q:\n%s", want, data)
	}
}

// streamServer serves the hub over a real listener so the follow stream can be read live.
func (th *testHub) streamServer(t *testing.T) *httptest.Server {
	t.Helper()
	th.hub.eventPoll = 10 * time.Millisecond
	th.hub.eventHeartbeat = 60 * time.Millisecond
	srv := httptest.NewServer(th.h)
	t.Cleanup(srv.Close)
	return srv
}

// readLines delivers each NDJSON line of the response on a channel until the body ends.
func readLines(resp *http.Response) <-chan map[string]interface{} {
	ch := make(chan map[string]interface{}, 64)
	go func() {
		defer close(ch)
		sc := bufio.NewScanner(resp.Body)
		for sc.Scan() {
			var m map[string]interface{}
			if json.Unmarshal(sc.Bytes(), &m) == nil {
				ch <- m
			}
		}
	}()
	return ch
}

func nextLine(t *testing.T, ch <-chan map[string]interface{}) map[string]interface{} {
	t.Helper()
	select {
	case m, ok := <-ch:
		if !ok {
			t.Fatal("stream closed early")
		}
		return m
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for stream line")
	}
	return nil
}

func TestDebateEventsFollowStream(t *testing.T) {
	th := newTestHub(t, nil)
	srv := th.streamServer(t)
	body := evLine(1, "debate_started") + evLine(2, "round_started")
	dir := th.seedEventsJob(t, eventsTestJob, "running", &body)

	resp, err := http.Get(srv.URL + "/api/debates/" + eventsTestJob + "/events?follow=1&after=1")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "application/x-ndjson" {
		t.Errorf("content-type %q", ct)
	}
	if resp.Header.Get("X-Accel-Buffering") != "no" || resp.Header.Get("Cache-Control") != "no-store" {
		t.Errorf("buffering/caching headers: %v", resp.Header)
	}
	lines := readLines(resp)

	if m := nextLine(t, lines); m["seq"] != float64(2) {
		t.Fatalf("existing line after=1: %v", m)
	}
	// Idle: a heartbeat arrives.
	if m := nextLine(t, lines); m["type"] != "heartbeat" {
		t.Fatalf("expected heartbeat, got %v", m)
	}
	// Append a whole line and a partial one; only the whole line is delivered.
	f, err := os.OpenFile(filepath.Join(dir, "events.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	_, _ = f.WriteString(evLine(3, "agent_started") + `{"seq":4,"type":"agent_fin`)
	var got map[string]interface{}
	for got = nextLine(t, lines); got["type"] == "heartbeat"; got = nextLine(t, lines) {
	}
	if got["seq"] != float64(3) {
		t.Fatalf("appended line: %v", got)
	}
	// Finish the partial line and end the job in the same breath: the drain must still
	// deliver seq 4 before the end message.
	_, _ = f.WriteString(`ished"}` + "\n")
	th.setStatus(t, eventsTestJob, "completed")
	var seq4, end bool
	for m := nextLine(t, lines); ; m = nextLine(t, lines) {
		if m["seq"] == float64(4) {
			seq4 = true
		}
		if m["type"] == "end" {
			end = true
			if m["status"] != "completed" {
				t.Errorf("end status %v", m["status"])
			}
			break
		}
	}
	if !seq4 || !end {
		t.Fatalf("seq4=%v end=%v", seq4, end)
	}
	select {
	case _, ok := <-lines:
		if ok {
			t.Error("data after end")
		}
	case <-time.After(5 * time.Second):
		t.Error("stream not closed after end")
	}
}

func TestDebateEventsFollowStopsOnClientDisconnect(t *testing.T) {
	th := newTestHub(t, nil)
	th.hub.eventPoll = 10 * time.Millisecond
	th.seedEventsJob(t, eventsTestJob, "running", nil)
	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest("GET", "/api/debates/"+eventsTestJob+"/events?follow=1", nil).WithContext(ctx)
	rec := httptest.NewRecorder()
	done := make(chan struct{})
	go func() { th.h.ServeHTTP(rec, req); close(done) }()
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("handler still running after client disconnect")
	}
}

func TestDebateEventsFollowCappedAtJobTimeout(t *testing.T) {
	th := newTestHub(t, func(c *debateConfig) { c.JobTimeout = 150 * time.Millisecond })
	th.hub.eventPoll = 10 * time.Millisecond
	th.seedEventsJob(t, eventsTestJob, "running", nil)
	start := time.Now()
	rec := th.call("GET", "/api/debates/"+eventsTestJob+"/events?follow=1", "")
	if time.Since(start) > 3*time.Second {
		t.Fatal("stream outlived the job timeout")
	}
	if strings.Contains(rec.Body.String(), `"end"`) {
		t.Errorf("running job must not get an end message: %q", rec.Body.String())
	}
}

func TestDebatePromptServesSavedFile(t *testing.T) {
	th := newTestHub(t, nil)
	ev := evLine(1, "agent_started")
	dir := th.seedEventsJob(t, eventsTestJob, "running", &ev)
	if err := os.MkdirAll(filepath.Join(dir, "prompts"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "prompts", "1.txt"), []byte("프롬프트 본문"), 0o600); err != nil {
		t.Fatal(err)
	}
	rec := th.call("GET", "/api/debates/"+eventsTestJob+"/prompts/1", "")
	if rec.Code != 200 {
		t.Fatalf("prompt: %d %s", rec.Code, rec.Body.String())
	}
	var out struct {
		Seq   int64  `json:"seq"`
		Bytes int    `json:"bytes"`
		Text  string `json:"text"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.Seq != 1 || out.Text != "프롬프트 본문" || out.Bytes != len("프롬프트 본문") {
		t.Fatalf("unexpected body: %+v", out)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("Cache-Control = %q", got)
	}
}

func TestDebatePromptRejectsBadInputAndLinks(t *testing.T) {
	th := newTestHub(t, nil)
	dir := th.seedEventsJob(t, eventsTestJob, "completed", nil)
	if err := os.MkdirAll(filepath.Join(dir, "prompts"), 0o700); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(th.dir, "secret.txt")
	if err := os.WriteFile(outside, []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(dir, "prompts", "2.txt")); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		path string
		code int
	}{
		{"/api/debates/" + eventsTestJob + "/prompts/abc", 400},
		{"/api/debates/" + eventsTestJob + "/prompts/-1", 400},
		{"/api/debates/" + eventsTestJob + "/prompts/1", 404},      // never saved
		{"/api/debates/" + eventsTestJob + "/prompts/2", 404},      // symlink is not followed
		{"/api/debates/dbt_20261008_120000_fff999/prompts/1", 404}, // unknown job
		{"/api/debates/..%2Fx/prompts/1", 400},
	}
	for _, c := range cases {
		rec := th.call("GET", c.path, "")
		if rec.Code != c.code {
			t.Errorf("%s: got %d, want %d (%s)", c.path, rec.Code, c.code, rec.Body.String())
		}
		if strings.Contains(rec.Body.String(), "secret") {
			t.Errorf("%s leaked the link target", c.path)
		}
	}
}

package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The command stream is POST-only with a JSON body; every rejection happens before any
// SSE header is written, so clients get a plain JSON error and no command runs.
func TestHandleFleetExecStreamRejectsBeforeStreaming(t *testing.T) {
	cases := []struct {
		name, method, body string
		code               int
	}{
		{"GET with query command", http.MethodGet, "", http.StatusMethodNotAllowed},
		{"invalid JSON", http.MethodPost, "{", http.StatusBadRequest},
		{"empty command", http.MethodPost, `{"command":"  "}`, http.StatusBadRequest},
		{"missing command", http.MethodPost, `{"targets":["a"]}`, http.StatusBadRequest},
		// timeout_sec with a string value is a JSON type mismatch; Decode returns an error → 400.
		{"non-numeric timeout_sec", http.MethodPost, `{"command":"id","timeout_sec":"fast"}`, http.StatusBadRequest},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req := httptest.NewRequest(c.method, "/api/fleet/exec/stream?cmd=id", strings.NewReader(c.body))
			rec := httptest.NewRecorder()
			handleFleetExecStream(rec, req)
			if rec.Code != c.code {
				t.Fatalf("code = %d, want %d", rec.Code, c.code)
			}
			if ct := rec.Header().Get("Content-Type"); strings.HasPrefix(ct, "text/event-stream") {
				t.Fatalf("stream started for a rejected request")
			}
		})
	}
}

// TestHandleFleetExecStreamForwardsTimeout verifies that timeout_sec from the request body
// reaches executeSSH unchanged. wg.Wait at StreamExecFleet:603 is the happens-before
// boundary that makes reading `got` safe after handleFleetExecStream returns.
func TestHandleFleetExecStreamForwardsTimeout(t *testing.T) {
	writeServers(t, `[{"id":"a","name":"n-a"}]`)

	var got int
	orig := streamSSHExec
	streamSSHExec = func(host, cmd string, timeout int) (int, string, string, int64) {
		got = timeout
		return 0, "ok", "", 5
	}
	t.Cleanup(func() { streamSSHExec = orig })

	cases := []struct {
		name, body string
		want       int
	}{
		{"explicit 42 targeted", `{"command":"id","targets":["a"],"timeout_sec":42}`, 42},
		{"explicit 7 broadcast", `{"command":"id","timeout_sec":7}`, 7},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got = 0
			req := httptest.NewRequest(http.MethodPost, "/api/fleet/exec/stream",
				strings.NewReader(c.body))
			handleFleetExecStream(httptest.NewRecorder(), req)
			if got != c.want {
				t.Errorf("executeSSH timeout = %d, want %d", got, c.want)
			}
		})
	}
}

// TestHandleFleetExecStreamDefaultsTimeout verifies that missing, zero, and negative
// timeout_sec all fall back to 10, matching ExecFleet (fleet_handler.go:366-368).
func TestHandleFleetExecStreamDefaultsTimeout(t *testing.T) {
	writeServers(t, `[{"id":"a","name":"n-a"}]`)

	var got int
	orig := streamSSHExec
	streamSSHExec = func(host, cmd string, timeout int) (int, string, string, int64) {
		got = timeout
		return 0, "ok", "", 5
	}
	t.Cleanup(func() { streamSSHExec = orig })

	cases := []struct {
		name, body string
	}{
		{"omitted", `{"command":"id","targets":["a"]}`},
		{"zero", `{"command":"id","targets":["a"],"timeout_sec":0}`},
		{"negative", `{"command":"id","targets":["a"],"timeout_sec":-5}`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got = 0
			req := httptest.NewRequest(http.MethodPost, "/api/fleet/exec/stream",
				strings.NewReader(c.body))
			handleFleetExecStream(httptest.NewRecorder(), req)
			if got != 10 {
				t.Errorf("timeout = %d, want 10", got)
			}
		})
	}
}

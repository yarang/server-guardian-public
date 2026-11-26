package main

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// cliCalls returns one line per fake CLI invocation (calls.log is appended by the fake itself).
func cliCalls(t *testing.T, th *testHub) []string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(th.dir, "calls.log"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	var calls []string
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if line != "" {
			calls = append(calls, line)
		}
	}
	return calls
}

func TestResidentMonitorRouteIsReadOnlyPassthrough(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/residents/monitor", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("monitor: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["agents"] == nil || m["problems"] == nil {
		t.Errorf("unexpected monitor output: %s", rec.Body.String())
	}
	if args := th.lastArgs(t); strings.Join(args, " ") != "--resident-monitor --json" {
		t.Errorf("CLI args: %q", args)
	}
	for _, method := range []string{"POST", "DELETE"} {
		if rec := th.call(method, "/api/residents/monitor", `{}`); rec.Code == http.StatusOK {
			t.Errorf("%s /api/residents/monitor: status 200, want not 200", method)
		}
	}
}

func TestResidentMailBodyRoute(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/residents/mail/m-20261121-0001", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("mail body: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["id"] != "m-20261121-0001" || m["masked"] != true {
		t.Errorf("unexpected mail body output: %s", rec.Body.String())
	}
	if args := th.lastArgs(t); strings.Join(args, " ") != "--resident-mail-body m-20261121-0001 --json" {
		t.Errorf("CLI args: %q", args)
	}
	// exit 3 from the CLI maps to 404.
	if rec := th.call("GET", "/api/residents/mail/m-20261121-0002", ""); rec.Code != http.StatusNotFound {
		t.Errorf("unknown mail: %d %s, want 404", rec.Code, rec.Body.String())
	}
}

func TestResidentMailBodyBadIDsRefusedBeforeCLI(t *testing.T) {
	th := newTestHub(t, nil)
	for _, id := range []string{
		"12", "-1", "m-2026-0001", "m-20261121-1", "M-20261121-0001", "m-20261121-0001x", "..%2Fetc",
	} {
		if rec := th.call("GET", "/api/residents/mail/"+id, ""); rec.Code != http.StatusBadRequest {
			t.Errorf("GET /api/residents/mail/%s: status %d %s, want 400", id, rec.Code, rec.Body.String())
		}
	}
	if calls := cliCalls(t, th); len(calls) != 0 {
		t.Errorf("CLI was invoked for bad ids: %v", calls)
	}
	for _, method := range []string{"POST", "DELETE"} {
		if rec := th.call(method, "/api/residents/mail/m-20261121-0001", `{}`); rec.Code == http.StatusOK {
			t.Errorf("%s /api/residents/mail/{id}: status 200, want not 200", method)
		}
	}
}

func TestResidentArtifactRoutes(t *testing.T) {
	th := newTestHub(t, nil)
	rec := th.call("GET", "/api/residents/artifacts", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("artifacts list: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["ok"] != true || m["artifacts"] == nil {
		t.Errorf("unexpected artifacts list: %s", rec.Body.String())
	}
	if args := th.lastArgs(t); strings.Join(args, " ") != "--resident-artifacts --json" {
		t.Errorf("CLI args: %q", args)
	}

	// JSON metadata + content
	rec = th.call("GET", "/api/residents/artifacts/a-0123456789ab", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("artifact get: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["id"] != "a-0123456789ab" || m["filename"] != "report.md" {
		t.Errorf("unexpected artifact payload: %s", rec.Body.String())
	}
	if args := th.lastArgs(t); strings.Join(args, " ") != "--resident-artifact-get a-0123456789ab --json" {
		t.Errorf("CLI args: %q", args)
	}

	// Raw download
	rec = th.call("GET", "/api/residents/artifacts/a-0123456789ab?download=1", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("artifact download: %d %s", rec.Code, rec.Body.String())
	}
	if got := rec.Body.String(); got != "hello" {
		t.Errorf("download body = %q, want %q", got, "hello")
	}
	if got := rec.Header().Get("X-Content-Type-Options"); got != "nosniff" {
		t.Errorf("X-Content-Type-Options = %q, want nosniff", got)
	}
	if got := rec.Header().Get("Content-Disposition"); !strings.Contains(got, `filename="report.md"`) {
		t.Errorf("Content-Disposition = %q, want report.md attachment", got)
	}

	// Unknown artifact maps exit 3 to 404
	if rec := th.call("GET", "/api/residents/artifacts/a-ffffffffffff", ""); rec.Code != http.StatusNotFound {
		t.Errorf("unknown artifact: %d %s, want 404", rec.Code, rec.Body.String())
	}
}

func TestResidentArtifactBadIDsRefusedBeforeCLI(t *testing.T) {
	th := newTestHub(t, nil)
	for _, id := range []string{"a-123", "A-0123456789ab", "a-0123456789ag", "x-0123456789ab", "..%2Fetc"} {
		if rec := th.call("GET", "/api/residents/artifacts/"+id, ""); rec.Code != http.StatusBadRequest {
			t.Errorf("GET /api/residents/artifacts/%s: status %d %s, want 400", id, rec.Code, rec.Body.String())
		}
	}
	if calls := cliCalls(t, th); len(calls) != 0 {
		t.Errorf("CLI was invoked for bad artifact ids: %v", calls)
	}
}

func TestResidentQueryRoutes(t *testing.T) {
	th := newTestHub(t, nil)

	// Submit -> 202 Accepted
	rec := th.call("POST", "/api/residents/queries", `{"address":"codex@node-1","project":"server-guardian","prompt":"status?"}`)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("query submit: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["id"] != "q-20261122-0001" || m["status"] != "queued" {
		t.Errorf("unexpected query submit output: %s", rec.Body.String())
	}
	if args := strings.Join(th.lastArgs(t), " "); args != "--resident-query codex@node-1 --project server-guardian --prompt-stdin --no-wait --json" {
		t.Errorf("CLI args = %q", args)
	}

	// Conflict (exit 4) maps to 409
	rec = th.call("POST", "/api/residents/queries", `{"address":"codex@node-1","project":"server-guardian","prompt":"NOACTIVE"}`)
	if rec.Code != http.StatusConflict {
		t.Errorf("no_active_session: %d %s, want 409", rec.Code, rec.Body.String())
	}

	// List with filters
	rec = th.call("GET", "/api/residents/queries?project=server-guardian&agent=codex@node-1&limit=10", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("query list: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["queries"] == nil {
		t.Errorf("unexpected query list: %s", rec.Body.String())
	}

	// Get detail
	rec = th.call("GET", "/api/residents/queries/q-20261122-0001", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("query get: %d %s", rec.Code, rec.Body.String())
	}
	if m := decode(t, rec); m["answer"] != "ok" {
		t.Errorf("unexpected query get: %s", rec.Body.String())
	}
	if rec := th.call("GET", "/api/residents/queries/q-20261122-9999", ""); rec.Code != http.StatusNotFound {
		t.Errorf("unknown query: %d %s, want 404", rec.Code, rec.Body.String())
	}

	// Cancel
	rec = th.call("DELETE", "/api/residents/queries/q-20261122-0001", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("query cancel: %d %s", rec.Code, rec.Body.String())
	}
}

func TestResidentQueryBadInputsRefusedBeforeCLI(t *testing.T) {
	th := newTestHub(t, nil)
	for _, body := range []string{
		`{"address":"bad","project":"server-guardian","prompt":"hi"}`,
		`{"address":"codex@node-1","project":"BAD!","prompt":"hi"}`,
		`{"address":"codex@node-1","project":"server-guardian","prompt":"   "}`,
		`{"address":"codex@node-1","project":"server-guardian","prompt":"hi","timeout_sec":5}`,
	} {
		if rec := th.call("POST", "/api/residents/queries", body); rec.Code != http.StatusBadRequest {
			t.Errorf("POST bad body %s: %d, want 400", body, rec.Code)
		}
	}
	for _, id := range []string{"q-123", "Q-20261122-0001", "..%2Fetc"} {
		if rec := th.call("GET", "/api/residents/queries/"+id, ""); rec.Code != http.StatusBadRequest {
			t.Errorf("GET bad query id %s: %d, want 400", id, rec.Code)
		}
		if rec := th.call("DELETE", "/api/residents/queries/"+id, ""); rec.Code != http.StatusBadRequest {
			t.Errorf("DELETE bad query id %s: %d, want 400", id, rec.Code)
		}
	}
	if calls := cliCalls(t, th); len(calls) != 0 {
		t.Errorf("CLI invoked for bad query inputs: %v", calls)
	}
}

func TestResidentMonitorRoutesRequireAuth(t *testing.T) {
	th := newTestHub(t, nil)
	h := withAuth(th.h, testToken)
	for _, path := range []string{
		"/api/residents/monitor",
		"/api/residents/mail/m-20261121-0001",
		"/api/residents/artifacts",
		"/api/residents/artifacts/a-0123456789ab",
		"/api/residents/queries",
		"/api/residents/queries/q-20261122-0001",
	} {
		if rec := do(h, "GET", path, "", nil); rec.Code != http.StatusUnauthorized {
			t.Errorf("GET %s without token: %d, want 401", path, rec.Code)
		}
		if rec := do(h, "GET", path, "", func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+testToken) }); rec.Code != http.StatusOK {
			t.Errorf("GET %s with token: %d %s", path, rec.Code, rec.Body.String())
		}
	}
}

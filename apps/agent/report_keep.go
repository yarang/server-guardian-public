package main

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// reportKeep stores the Markdown report of a finished debate as a `report.md` artifact version
// (docs/design/office-export.md, D1 option (c)). It is the only way a report enters the artifact store: a person
// presses the dashboard button, the CLI re-reads the report on disk itself (no text travels through this request),
// validates it like any artifact and writes an immutable version. The Word and PowerPoint conversions then work on it.
func (h *debateHub) reportKeep(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	project, _, ok := h.findReport(thread)
	if !ok {
		errJSON(w, http.StatusNotFound, "report not found")
		return
	}
	code, out, errOut, err := h.runCLI(h.cfg.actionTimeout(), nil, "--report-keep", "--project", project, "--thread", thread)
	var result struct {
		Version int    `json:"version"`
		SHA256  string `json:"sha256"`
	}
	if code == 0 && err == nil {
		_ = json.Unmarshal(out, &result)
	}
	status, detail := "ok", "version=v"+strconv.Itoa(result.Version)+" sha256="+result.SHA256
	if err != nil || code != 0 {
		status, detail = "error", joinErr("exit "+strconv.Itoa(code), tailString(strings.TrimSpace(errOut+string(out)), 300))
		if err != nil {
			detail = err.Error()
		}
	}
	recordAudit(r, "POST /api/threads/{thread}/report/keep", []string{project}, "report keep "+thread, status, detail,
		map[string]int{"keep": code}, start)
	h.respondConfig(w, code, out, errOut, err)
}

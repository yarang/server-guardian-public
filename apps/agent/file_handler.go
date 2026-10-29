package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// File view API (docs/design/code-wiki-coverage.md, C): one repository file of a project as the
// dashboard shows it, made without an LLM by the guardian.py file commands (cli/llm-agent/file_view.py).
// The CLI decides what is readable (no secret patterns, symlinks, submodules or binary files) and
// answers 404 (exit 3) for anything else. Reading lines is the first route that hands repository
// source to the dashboard, so every lines request is written to the audit log (GET /api/audit).

const (
	maxFilePathBytes = 1024
	maxFileReadLines = 400
	routeFileLines   = "GET /api/projects/{id}/files/lines"
)

func (h *debateHub) registerFileRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/projects/{id}/files", h.guard(h.fileList))
	mux.HandleFunc("GET /api/projects/{id}/files/show", h.guard(h.fileShow))
	mux.HandleFunc("GET /api/projects/{id}/files/lines", h.guard(h.fileLines))
}

// validRepoPath accepts a repository-relative path: non-empty, at most maxFilePathBytes, no control
// characters, not absolute, no leading "-", and no "." or ".." segments. Whether the file exists and
// may be read is the CLI's decision.
func validRepoPath(p string) bool {
	if p == "" || len(p) > maxFilePathBytes || strings.HasPrefix(p, "/") || strings.HasPrefix(p, "-") {
		return false
	}
	for _, r := range p {
		if unicode.IsControl(r) {
			return false
		}
	}
	for _, seg := range strings.Split(p, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return false
		}
	}
	return true
}

func filePathParam(w http.ResponseWriter, r *http.Request) (string, bool) {
	p := r.URL.Query().Get("path")
	if !validRepoPath(p) {
		errJSON(w, http.StatusBadRequest, "invalid path")
		return "", false
	}
	return p, true
}

func (h *debateHub) fileList(w http.ResponseWriter, r *http.Request) {
	project, sha, ok := wikiParams(w, r, true)
	if !ok {
		return
	}
	args := []string{"--code-file-list", "--project=" + project, "--json"}
	if sha != "" {
		args = append(args, "--sha="+sha)
	}
	code, out, errOut, err := h.runCLI(wikiCLITimeout, nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

func (h *debateHub) fileShow(w http.ResponseWriter, r *http.Request) {
	project, sha, ok := wikiParams(w, r, true)
	if !ok {
		return
	}
	p, ok := filePathParam(w, r)
	if !ok {
		return
	}
	args := []string{"--code-file-show=" + p, "--project=" + project, "--json"}
	if sha != "" {
		args = append(args, "--sha="+sha)
	}
	code, out, errOut, err := h.runCLI(wikiCLITimeout, nil, args...)
	h.respondConfig(w, code, out, errOut, err)
}

// lineParam reads a 1-based line number query value; missing is def.
func lineParam(r *http.Request, name string, def int) (int, bool) {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return def, true
	}
	n, err := strconv.Atoi(raw)
	return n, err == nil && n >= 1
}

func (h *debateHub) fileLines(w http.ResponseWriter, r *http.Request) {
	begin := time.Now()
	project, sha, ok := wikiParams(w, r, true)
	if !ok {
		return
	}
	p, ok := filePathParam(w, r)
	if !ok {
		return
	}
	start, okStart := lineParam(r, "start", 1)
	end, okEnd := lineParam(r, "end", start+maxFileReadLines-1)
	if !okStart || !okEnd || end < start || end-start+1 > maxFileReadLines {
		errJSON(w, http.StatusBadRequest, fmt.Sprintf("start and end must be line numbers, start <= end, at most %d lines", maxFileReadLines))
		return
	}
	args := []string{"--code-file-read=" + p, "--start=" + strconv.Itoa(start), "--end=" + strconv.Itoa(end),
		"--project=" + project, "--json"}
	if sha != "" {
		args = append(args, "--sha="+sha)
	}
	code, out, errOut, err := h.runCLI(wikiCLITimeout, nil, args...)
	status, detail := "ok", ""
	if err != nil || code != 0 {
		status = "error"
		var body struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(out, &body) == nil {
			detail = body.Error
		}
		if err != nil {
			detail = err.Error()
		}
	}
	// The audit line names what was read, never the content.
	recordAudit(r, routeFileLines, []string{project}, fmt.Sprintf("read %s:%d-%d", p, start, end), status, detail,
		map[string]int{project: code}, begin)
	h.respondConfig(w, code, out, errOut, err)
}

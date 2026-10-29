package main

import (
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// Project results for the dashboard's Results tab: one list that merges what a project produced, whatever its kind.
// Every finished debate thread has a Markdown report under <data dir>/decisions/projects/<project>/<thread>.md
// (debate_report.py); a design project also has versioned artifacts under <data dir>/design-artifacts/<project>/
// <thread>/ (artifact_ops.py). The list joins both by thread id so the dashboard can open either one. Like the
// report and artifact routes it reads straight from disk, is read-only and needs no CLI.
//
// The report head is data on disk, so the list only trusts what it can bound: the title is cut at resultTitleRunes
// and stripped of control characters, the status must be a plain upper-case word and the rounds a small number.
const (
	// maxResultItems caps one response. The newest are kept; total tells the dashboard that more exist.
	maxResultItems   = 200
	resultHeadBytes  = 4096
	resultTitleRunes = 120
	resultMaxRounds  = 999
	reportTitleLead  = "# 토론 결과:"
)

var (
	resultStatusPattern = regexp.MustCompile(`(?m)^\|\s*결정 상태\s*\|\s*([A-Z][A-Z_]{0,23})\b`)
	resultRoundsPattern = regexp.MustCompile(`(?m)^\|\s*라운드\s*\|\s*([0-9]{1,3})\s*\|`)
	// reportFilePattern is a thread id plus ".md"; threadPattern alone would also match "thread_x.assets".
	reportFilePattern = regexp.MustCompile(`^(thread_[A-Za-z0-9_]{1,80})\.md$`)
)

type resultItem struct {
	ThreadID         string `json:"thread_id"`
	Title            string `json:"title"`
	Status           string `json:"status"`
	Rounds           int    `json:"rounds"`
	UpdatedAt        string `json:"updated_at"`
	HasReport        bool   `json:"has_report"`
	ReportBytes      int64  `json:"report_bytes"`
	ArtifactVersions int    `json:"artifact_versions"`
	ArtifactFiles    int    `json:"artifact_files"`
}

func (h *debateHub) registerResultRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/projects/{id}/results", h.projectResults)
}

// cleanResultText drops control and format characters (bidirectional overrides included), collapses whitespace and
// cuts the text at limit runes.
func cleanResultText(s string, limit int) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case unicode.IsSpace(r):
			b.WriteRune(' ')
		case unicode.IsControl(r), unicode.Is(unicode.Cf, r), r == unicode.ReplacementChar:
		default:
			b.WriteRune(r)
		}
	}
	text := strings.TrimSpace(b.String())
	if runes := []rune(text); len(runes) > limit {
		text = strings.TrimSpace(string(runes[:limit])) + "…"
	}
	return text
}

// readReportHead reads the first resultHeadBytes of a report without following a symlink.
func readReportHead(path string) (head string, size int64, mtime time.Time, ok bool) {
	f, st, ok := openRegular(path)
	if !ok {
		return
	}
	defer f.Close()
	buf, err := io.ReadAll(io.LimitReader(f, resultHeadBytes))
	if err != nil {
		return "", 0, time.Time{}, false
	}
	return string(buf), st.Size(), st.ModTime(), true
}

// parseReportHead picks the title, status word and round count out of a report head; each falls back to its zero value.
func parseReportHead(head string) (title, status string, rounds int) {
	first := head
	if i := strings.IndexByte(head, '\n'); i >= 0 {
		first = head[:i]
	}
	if strings.HasPrefix(first, reportTitleLead) {
		title = cleanResultText(strings.TrimPrefix(first, reportTitleLead), resultTitleRunes)
	}
	if m := resultStatusPattern.FindStringSubmatch(head); m != nil {
		status = m[1]
	}
	if m := resultRoundsPattern.FindStringSubmatch(head); m != nil {
		if n, err := strconv.Atoi(m[1]); err == nil && n <= resultMaxRounds {
			rounds = n
		}
	}
	return
}

func (h *debateHub) projectResults(w http.ResponseWriter, r *http.Request) {
	project := r.PathValue("id")
	if !idPattern.MatchString(project) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	byThread := map[string]*resultItem{}
	times := map[string]time.Time{}

	decisions := filepath.Join(h.cfg.DataDir, decisionsDirName)
	rdir := h.projectDir(project)
	if isRealDir(decisions) && isRealDir(filepath.Join(decisions, projectsDirName)) && isRealDir(rdir) {
		entries, _ := os.ReadDir(rdir)
		for _, e := range entries {
			m := reportFilePattern.FindStringSubmatch(e.Name())
			// Only regular files: opening a FIFO named like a report would block the request before openRegular can
			// check its type, and symlinks are refused here as well as inside openRegular.
			if m == nil || !e.Type().IsRegular() {
				continue
			}
			head, size, mtime, ok := readReportHead(filepath.Join(rdir, e.Name()))
			if !ok {
				continue
			}
			title, status, rounds := parseReportHead(head)
			byThread[m[1]] = &resultItem{
				ThreadID: m[1], Title: title, Status: status, Rounds: rounds, HasReport: true, ReportBytes: size,
			}
			times[m[1]] = mtime
		}
	}

	adir := filepath.Join(h.artifactsRoot(), project)
	if isRealDir(h.artifactsRoot()) && isRealDir(adir) {
		entries, _ := os.ReadDir(adir)
		for _, e := range entries {
			name := e.Name()
			tdir := filepath.Join(adir, name)
			if !threadPattern.MatchString(name) || len(name) > 80 || !isRealDir(tdir) {
				continue
			}
			versions := artifactVersionNumbers(tdir)
			if len(versions) == 0 {
				continue
			}
			latest, ok := readArtifactManifest(tdir, versions[len(versions)-1])
			if !ok {
				continue
			}
			it := byThread[name]
			if it == nil {
				it = &resultItem{ThreadID: name}
				byThread[name] = it
			}
			s := summarizeArtifact(latest)
			it.ArtifactVersions, it.ArtifactFiles = len(versions), s.Files
			if at, err := time.Parse(time.RFC3339, latest.CreatedAt); err == nil && at.After(times[name]) {
				times[name] = at
			}
		}
	}

	items := make([]*resultItem, 0, len(byThread))
	for id, it := range byThread {
		if it.Title == "" {
			it.Title = id
		}
		if t := times[id]; !t.IsZero() {
			it.UpdatedAt = t.UTC().Format(time.RFC3339)
		}
		items = append(items, it)
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].UpdatedAt != items[j].UpdatedAt {
			return items[i].UpdatedAt > items[j].UpdatedAt
		}
		return items[i].ThreadID > items[j].ThreadID
	})
	total := len(items)
	if total > maxResultItems {
		items = items[:maxResultItems]
	}
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, map[string]interface{}{"project": project, "items": items, "total": total})
}

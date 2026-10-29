package main

import (
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"syscall"
	"time"
)

// Thread reports for the dashboard: the Python CLI writes the Markdown report of a debate
// thread and its SVG assets under <data dir>/decisions/projects/<project>/ ; the hub only
// serves them read-only (like the event stream, this needs no CLI and is not guarded).
const (
	decisionsDirName = "decisions"
	projectsDirName  = "projects"
	assetsDirSuffix  = ".assets"
	maxReportMD      = 8 << 20
	maxReportAsset   = 1 << 20
)

var reportAssetPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}\.svg$`)

type reportAsset struct {
	Name  string `json:"name"`
	Bytes int64  `json:"bytes"`
}

// isRealDir reports whether path is a directory itself (a symlink to one does not count).
func isRealDir(path string) bool {
	st, err := os.Lstat(path)
	return err == nil && st.Mode().IsDir()
}

// openRegular opens path without following a final symlink and requires a regular file.
func openRegular(path string) (*os.File, os.FileInfo, bool) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, nil, false
	}
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		f.Close()
		return nil, nil, false
	}
	return f, st, true
}

// findReport locates the newest <thread>.md among the project directories. Only project names
// that satisfy idPattern and are real directories are considered.
func (h *debateHub) findReport(thread string) (project string, mtime time.Time, ok bool) {
	root := filepath.Join(h.cfg.DataDir, decisionsDirName, projectsDirName)
	if !isRealDir(filepath.Join(h.cfg.DataDir, decisionsDirName)) || !isRealDir(root) {
		return
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return
	}
	for _, e := range entries {
		name := e.Name()
		if !idPattern.MatchString(name) || !isRealDir(filepath.Join(root, name)) {
			continue
		}
		st, err := os.Lstat(filepath.Join(root, name, thread+".md"))
		if err != nil || !st.Mode().IsRegular() {
			continue
		}
		if !ok || st.ModTime().After(mtime) {
			project, mtime, ok = name, st.ModTime(), true
		}
	}
	return
}

func (h *debateHub) projectDir(project string) string {
	return filepath.Join(h.cfg.DataDir, decisionsDirName, projectsDirName, project)
}

// threadReport serves the Markdown report of a thread together with its asset list.
func (h *debateHub) threadReport(w http.ResponseWriter, r *http.Request) {
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
	dir := h.projectDir(project)
	f, st, ok := openRegular(filepath.Join(dir, thread+".md"))
	if !ok {
		errJSON(w, http.StatusNotFound, "report not found")
		return
	}
	defer f.Close()
	if st.Size() > maxReportMD {
		errJSON(w, http.StatusRequestEntityTooLarge, "report too large")
		return
	}
	data, err := io.ReadAll(io.LimitReader(f, maxReportMD+1))
	if err != nil {
		errJSON(w, http.StatusInternalServerError, "cannot read report")
		return
	}
	if len(data) > maxReportMD {
		errJSON(w, http.StatusRequestEntityTooLarge, "report too large")
		return
	}
	assets := []reportAsset{}
	adir := filepath.Join(dir, thread+assetsDirSuffix)
	if isRealDir(adir) {
		if entries, err := os.ReadDir(adir); err == nil {
			for _, e := range entries {
				if !reportAssetPattern.MatchString(e.Name()) {
					continue
				}
				ast, err := os.Lstat(filepath.Join(adir, e.Name()))
				if err != nil || !ast.Mode().IsRegular() {
					continue
				}
				assets = append(assets, reportAsset{Name: e.Name(), Bytes: ast.Size()})
			}
		}
		sort.Slice(assets, func(i, j int) bool { return assets[i].Name < assets[j].Name })
	}
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, map[string]interface{}{
		"thread_id":  thread,
		"project":    project,
		"markdown":   string(data),
		"bytes":      len(data),
		"updated_at": st.ModTime().UTC().Format(time.RFC3339),
		"assets":     assets,
	})
}

// threadReportAsset serves one SVG of a thread report as JSON text (an <img> cannot carry the
// dashboard's auth header, so the client fetches the text and renders it itself).
func (h *debateHub) threadReportAsset(w http.ResponseWriter, r *http.Request) {
	thread, name := r.PathValue("thread"), r.PathValue("name")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	if !reportAssetPattern.MatchString(name) {
		errJSON(w, http.StatusBadRequest, "invalid asset name")
		return
	}
	project, _, ok := h.findReport(thread)
	if !ok {
		errJSON(w, http.StatusNotFound, "asset not found")
		return
	}
	adir := filepath.Join(h.projectDir(project), thread+assetsDirSuffix)
	if !isRealDir(adir) {
		errJSON(w, http.StatusNotFound, "asset not found")
		return
	}
	f, st, ok := openRegular(filepath.Join(adir, name))
	if !ok {
		errJSON(w, http.StatusNotFound, "asset not found")
		return
	}
	defer f.Close()
	if st.Size() > maxReportAsset {
		errJSON(w, http.StatusRequestEntityTooLarge, "asset too large")
		return
	}
	data, err := io.ReadAll(io.LimitReader(f, maxReportAsset+1))
	if err != nil {
		errJSON(w, http.StatusInternalServerError, "cannot read asset")
		return
	}
	if len(data) > maxReportAsset {
		errJSON(w, http.StatusRequestEntityTooLarge, "asset too large")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, map[string]interface{}{"name": name, "bytes": len(data), "text": string(data)})
}

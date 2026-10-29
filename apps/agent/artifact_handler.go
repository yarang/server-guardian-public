package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Design artifacts for the dashboard (docs/design/design-project.md, "Viewing and download" and "Security").
// cli/llm-agent/artifact_ops.py writes <data dir>/design-artifacts/<project>/<thread>/vN/ (a full snapshot per version
// with manifest.json); the hub only reads it, directly from disk like the thread reports, so these routes need no CLI.
//
// What is served is decided here, not by the files: a name must be listed in the manifest, not expired, match the
// artifact name pattern and have an extension of the table below (the table, not the manifest, picks the
// Content-Type). Every directory level is checked for symlinks, the bytes are hashed and compared with the manifest,
// and every response carries a sandbox CSP, so an artifact opened directly in a tab can never run script with the
// dashboard's origin and cookie.

const (
	artifactsDirName = "design-artifacts"
	// maxArtifactFile mirrors the 2 MiB safety limit of artifact_ops.py (DEFAULT_SAFETY_BYTES); a larger file on disk
	// was not written by the store and is refused.
	maxArtifactFile     = 2 << 20
	maxArtifactManifest = 1 << 20
	maxArtifactBundle   = 64 << 20
	artifactCSP         = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'"
)

var (
	// artifactNamePattern is NAME_PATTERN of artifact_ops.py.
	artifactNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,80}$`)
	// artifactVersionPattern is _VERSION_DIR of artifact_ops.py plus the leading "v".
	artifactVersionPattern = regexp.MustCompile(`^v[1-9][0-9]{0,8}$`)
)

// artifactTypes is EXTENSIONS and MIME of artifact_ops.py; it is a gate (anything else is not served, so a
// tampered manifest cannot expose manifest.json or another file of the directory) and the source of Content-Type.
var artifactTypes = map[string]struct{ kind, mime string }{
	".md":  {"markdown", "text/markdown; charset=utf-8"},
	".mmd": {"mermaid", "text/plain; charset=utf-8"},
	".svg": {"svg", "image/svg+xml"},
	// Derived by --artifact-convert (docs/design/office-export.md); never inline, see artifactAttachmentOnly.
	".docx": {"docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
	".pptx": {"pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
}

// artifactConvertFormats are the --artifact-format values of artifact_ops.CONVERT_TARGETS.
var artifactConvertFormats = map[string]bool{"docx": true, "pptx": true}

// artifactAttachmentOnly lists the kinds that are only ever served with Content-Disposition: attachment, with or
// without ?download=1: an Office file is opened by the person's own application, never rendered by the dashboard.
func artifactAttachmentOnly(name string) bool {
	return artifactConvertFormats[artifactTypes[strings.ToLower(filepath.Ext(name))].kind]
}

// artifactMime returns the Content-Type for a file name, or false when its extension is not served.
func artifactMime(name string) (string, bool) {
	t, ok := artifactTypes[strings.ToLower(filepath.Ext(name))]
	return t.mime, ok
}

type artifactFile struct {
	Path    string `json:"path"`
	SHA256  string `json:"sha256"`
	Bytes   int64  `json:"bytes"`
	Mime    string `json:"mime"`
	Kind    string `json:"kind"`
	Stage   string `json:"stage"`
	Agent   string `json:"agent"`
	Title   string `json:"title"`
	Expired bool   `json:"expired,omitempty"`
	// Source and SourceSHA256 are set on a derived file: the stored file it was made from and that file's hash.
	Source       string `json:"source,omitempty"`
	SourceSHA256 string `json:"source_sha256,omitempty"`
	ConvertedAt  string `json:"converted_at,omitempty"`
}

type artifactManifest struct {
	ManifestVersion int            `json:"manifest_version"`
	Version         int            `json:"version"`
	Project         string         `json:"project"`
	Thread          string         `json:"thread"`
	CreatedAt       string         `json:"created_at"`
	Parent          *int           `json:"parent"`
	Written         string         `json:"written"`
	ExpiredAt       string         `json:"expired_at,omitempty"`
	Files           []artifactFile `json:"files"`
}

func (h *debateHub) registerArtifactRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/projects/{id}/artifacts", h.artifactProjectList)
	mux.HandleFunc("GET /api/threads/{thread}/artifacts", h.artifactVersions)
	mux.HandleFunc("GET /api/threads/{thread}/artifacts/{version}", h.artifactManifestShow)
	mux.HandleFunc("GET /api/threads/{thread}/artifacts/{version}/files/{name}", h.artifactFile)
	mux.HandleFunc("GET /api/threads/{thread}/artifacts/{version}/bundle.zip", h.artifactBundle)
	mux.HandleFunc("POST /api/threads/{thread}/artifacts/{version}/convert", h.guard(h.artifactConvert))
	mux.HandleFunc("POST /api/threads/{thread}/report/keep", h.guard(h.reportKeep))
}

func (h *debateHub) artifactsRoot() string {
	return filepath.Join(h.cfg.DataDir, artifactsDirName)
}

// artifactVersionNumber parses "v12" with the same rule as the store (no leading zero, at most nine digits).
func artifactVersionNumber(v string) (int, bool) {
	if !artifactVersionPattern.MatchString(v) {
		return 0, false
	}
	n, err := strconv.Atoi(v[1:])
	return n, err == nil && n > 0
}

// artifactThreadDir finds the project directory that holds a thread. Every level must be a real directory.
// The store truncates ids to 80 characters (safe_id), so a longer thread id cannot exist on disk.
func (h *debateHub) artifactThreadDir(thread string) (project, dir string, ok bool) {
	if !threadPattern.MatchString(thread) || len(thread) > 80 {
		return "", "", false
	}
	root := h.artifactsRoot()
	if !isRealDir(root) {
		return "", "", false
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return "", "", false
	}
	var newest time.Time
	for _, e := range entries {
		name := e.Name()
		cand := filepath.Join(root, name, thread)
		if !idPattern.MatchString(name) || !isRealDir(filepath.Join(root, name)) || !isRealDir(cand) {
			continue
		}
		st, err := os.Lstat(cand)
		if err != nil {
			continue
		}
		if !ok || st.ModTime().After(newest) {
			project, dir, newest, ok = name, cand, st.ModTime(), true
		}
	}
	return
}

// artifactVersionNumbers lists the complete versions of a thread directory (a real vN directory whose manifest.json is
// a regular file), ascending. Staging directories (.tmp-v*) and anything else are ignored.
func artifactVersionNumbers(threadDir string) []int {
	entries, err := os.ReadDir(threadDir)
	if err != nil {
		return nil
	}
	var found []int
	for _, e := range entries {
		n, ok := artifactVersionNumber(e.Name())
		if !ok || !isRealDir(filepath.Join(threadDir, e.Name())) {
			continue
		}
		if st, err := os.Lstat(filepath.Join(threadDir, e.Name(), "manifest.json")); err != nil || !st.Mode().IsRegular() {
			continue
		}
		found = append(found, n)
	}
	sort.Ints(found)
	return found
}

// readArtifactManifest loads vN/manifest.json. A manifest is data on disk: entries whose path is not a plain artifact
// name or whose extension is not served are dropped, and the version number must match the directory.
func readArtifactManifest(threadDir string, n int) (*artifactManifest, bool) {
	vdir := filepath.Join(threadDir, "v"+strconv.Itoa(n))
	if !isRealDir(vdir) {
		return nil, false
	}
	f, st, ok := openRegular(filepath.Join(vdir, "manifest.json"))
	if !ok {
		return nil, false
	}
	defer f.Close()
	if st.Size() > maxArtifactManifest {
		return nil, false
	}
	var m artifactManifest
	if err := json.NewDecoder(io.LimitReader(f, maxArtifactManifest)).Decode(&m); err != nil || m.Version != n {
		return nil, false
	}
	kept := make([]artifactFile, 0, len(m.Files))
	for _, af := range m.Files {
		if _, served := artifactMime(af.Path); !served || !artifactNamePattern.MatchString(af.Path) {
			continue
		}
		af.Mime, _ = artifactMime(af.Path) // never the manifest's own value
		kept = append(kept, af)
	}
	m.Files = kept
	return &m, true
}

// artifactSummary is one version in a listing.
type artifactSummary struct {
	Version   int    `json:"version"`
	CreatedAt string `json:"created_at"`
	Written   string `json:"written"`
	Files     int    `json:"files"`
	Bytes     int64  `json:"bytes"`
	ExpiredAt string `json:"expired_at,omitempty"`
}

func summarizeArtifact(m *artifactManifest) artifactSummary {
	s := artifactSummary{Version: m.Version, CreatedAt: m.CreatedAt, Written: m.Written, ExpiredAt: m.ExpiredAt}
	for _, f := range m.Files {
		if !f.Expired {
			s.Files++
			s.Bytes += f.Bytes
		}
	}
	return s
}

// artifactProjectList answers the latest version of every thread of a project.
func (h *debateHub) artifactProjectList(w http.ResponseWriter, r *http.Request) {
	project := r.PathValue("id")
	if !idPattern.MatchString(project) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	threads := []map[string]interface{}{}
	pdir := filepath.Join(h.artifactsRoot(), project)
	if isRealDir(h.artifactsRoot()) && isRealDir(pdir) {
		entries, _ := os.ReadDir(pdir)
		for _, e := range entries {
			if !threadPattern.MatchString(e.Name()) || len(e.Name()) > 80 || !isRealDir(filepath.Join(pdir, e.Name())) {
				continue
			}
			tdir := filepath.Join(pdir, e.Name())
			versions := artifactVersionNumbers(tdir)
			if len(versions) == 0 {
				continue
			}
			m, ok := readArtifactManifest(tdir, versions[len(versions)-1])
			if !ok {
				continue
			}
			threads = append(threads, map[string]interface{}{
				"thread_id": e.Name(), "versions": len(versions), "latest": summarizeArtifact(m),
			})
		}
	}
	sort.Slice(threads, func(i, j int) bool {
		return threads[i]["latest"].(artifactSummary).CreatedAt > threads[j]["latest"].(artifactSummary).CreatedAt
	})
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, map[string]interface{}{"project": project, "threads": threads})
}

// artifactVersions lists every version of a thread; expired versions stay listed with expired_at (their manifest is a tombstone).
func (h *debateHub) artifactVersions(w http.ResponseWriter, r *http.Request) {
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	project, dir, ok := h.artifactThreadDir(thread)
	if !ok {
		errJSON(w, http.StatusNotFound, "artifacts not found")
		return
	}
	versions := []artifactSummary{}
	for _, n := range artifactVersionNumbers(dir) {
		if m, ok := readArtifactManifest(dir, n); ok {
			versions = append(versions, summarizeArtifact(m))
		}
	}
	if len(versions) == 0 {
		errJSON(w, http.StatusNotFound, "artifacts not found")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, map[string]interface{}{"thread_id": thread, "project": project, "versions": versions})
}

// artifactVersionParams resolves {thread} and {version} to the manifest of an existing version.
func (h *debateHub) artifactVersionParams(w http.ResponseWriter, r *http.Request) (dir string, m *artifactManifest, ok bool) {
	thread := r.PathValue("thread")
	if !threadPattern.MatchString(thread) {
		errJSON(w, http.StatusBadRequest, "invalid thread")
		return
	}
	n, valid := artifactVersionNumber(r.PathValue("version"))
	if !valid {
		errJSON(w, http.StatusBadRequest, "invalid version")
		return
	}
	_, tdir, found := h.artifactThreadDir(thread)
	if !found {
		errJSON(w, http.StatusNotFound, "artifacts not found")
		return
	}
	mf, found := readArtifactManifest(tdir, n)
	if !found {
		errJSON(w, http.StatusNotFound, "version not found")
		return
	}
	return filepath.Join(tdir, "v"+strconv.Itoa(n)), mf, true
}

func (h *debateHub) artifactManifestShow(w http.ResponseWriter, r *http.Request) {
	_, m, ok := h.artifactVersionParams(w, r)
	if !ok {
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, m)
}

// loadArtifact reads one listed, unexpired file of a version and verifies it against the manifest.
func loadArtifact(vdir string, af artifactFile) ([]byte, int) {
	if af.Expired {
		return nil, http.StatusNotFound
	}
	f, st, ok := openRegular(filepath.Join(vdir, af.Path))
	if !ok {
		return nil, http.StatusNotFound
	}
	defer f.Close()
	if st.Size() > maxArtifactFile {
		return nil, http.StatusRequestEntityTooLarge
	}
	data, err := io.ReadAll(io.LimitReader(f, maxArtifactFile+1))
	if err != nil {
		return nil, http.StatusInternalServerError
	}
	if len(data) > maxArtifactFile {
		return nil, http.StatusRequestEntityTooLarge
	}
	sum := sha256.Sum256(data)
	if !strings.EqualFold(hex.EncodeToString(sum[:]), af.SHA256) || int64(len(data)) != af.Bytes {
		return nil, http.StatusConflict // the file no longer matches what the store recorded
	}
	return data, 0
}

// setArtifactHeaders applies the fixed headers of every artifact file response.
func setArtifactHeaders(w http.ResponseWriter, contentType string) {
	hd := w.Header()
	hd.Set("Content-Type", contentType)
	hd.Set("Content-Security-Policy", artifactCSP)
	hd.Set("X-Content-Type-Options", "nosniff")
	hd.Set("Referrer-Policy", "no-referrer")
	hd.Set("Cache-Control", "private, no-cache")
}

func auditArtifact(r *http.Request, route, project, command string, start time.Time) {
	recordAudit(r, route, []string{project}, command, "ok", "", nil, start)
}

// artifactFile serves one file of a version: attachment with ?download=1, ETag = sha256, Range through ServeContent.
func (h *debateHub) artifactFile(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	dir, m, ok := h.artifactVersionParams(w, r)
	if !ok {
		return
	}
	name := r.PathValue("name")
	if !artifactNamePattern.MatchString(name) {
		errJSON(w, http.StatusBadRequest, "invalid file name")
		return
	}
	contentType, served := artifactMime(name)
	if !served {
		errJSON(w, http.StatusNotFound, "file not found")
		return
	}
	var entry *artifactFile
	for i := range m.Files {
		if m.Files[i].Path == name {
			entry = &m.Files[i]
			break
		}
	}
	if entry == nil {
		errJSON(w, http.StatusNotFound, "file not found")
		return
	}
	data, status := loadArtifact(dir, *entry)
	if status != 0 {
		switch status {
		case http.StatusConflict:
			errJSON(w, status, "file does not match its manifest")
		case http.StatusRequestEntityTooLarge:
			errJSON(w, status, "file too large")
		case http.StatusInternalServerError:
			errJSON(w, status, "cannot read file")
		default:
			errJSON(w, status, "file not found")
		}
		return
	}
	setArtifactHeaders(w, contentType)
	w.Header().Set("ETag", `"`+entry.SHA256+`"`)
	if r.URL.Query().Get("download") == "1" || artifactAttachmentOnly(name) {
		w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name}))
	}
	auditArtifact(r, "GET /api/threads/{thread}/artifacts/{version}/files/{name}", m.Project, "artifact "+name+"@v"+strconv.Itoa(m.Version), start)
	http.ServeContent(w, r, name, time.Time{}, bytes.NewReader(data))
}

// artifactBundle streams every unexpired file plus manifest.json as a zip. All files are read and verified before the
// first byte is written, so a bad file is a clean error status, not a truncated archive.
func (h *debateHub) artifactBundle(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	dir, m, ok := h.artifactVersionParams(w, r)
	if !ok {
		return
	}
	type member struct {
		name string
		data []byte
	}
	var members []member
	var total int64
	for _, af := range m.Files {
		if af.Expired {
			continue
		}
		data, status := loadArtifact(dir, af)
		if status != 0 {
			errJSON(w, status, "file "+af.Path+" cannot be bundled")
			return
		}
		if total += int64(len(data)); total > maxArtifactBundle {
			errJSON(w, http.StatusRequestEntityTooLarge, "bundle too large")
			return
		}
		members = append(members, member{af.Path, data})
	}
	if len(members) == 0 {
		errJSON(w, http.StatusNotFound, "no files in this version")
		return
	}
	manifestJSON, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		errJSON(w, http.StatusInternalServerError, "cannot build bundle")
		return
	}
	hd := w.Header()
	hd.Set("Content-Type", "application/zip")
	hd.Set("Content-Security-Policy", artifactCSP)
	hd.Set("X-Content-Type-Options", "nosniff")
	hd.Set("Referrer-Policy", "no-referrer")
	hd.Set("Cache-Control", "private, no-cache")
	hd.Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": m.Thread + "-v" + strconv.Itoa(m.Version) + ".zip"}))
	auditArtifact(r, "GET /api/threads/{thread}/artifacts/{version}/bundle.zip", m.Project, "artifact bundle @v"+strconv.Itoa(m.Version), start)
	zw := zip.NewWriter(w)
	defer zw.Close()
	stamp := time.Now().UTC()
	add := func(name string, data []byte) error {
		// Text artifacts compress well; the entries are deflated and carry a fixed timestamp.
		fw, err := zw.CreateHeader(&zip.FileHeader{Name: name, Method: zip.Deflate, Modified: stamp})
		if err != nil {
			return err
		}
		_, err = fw.Write(data)
		return err
	}
	for _, mem := range members {
		if add(mem.name, mem.data) != nil {
			return
		}
	}
	_ = add("manifest.json", manifestJSON)
}

// artifactConvertRequest is the body of the convert route: which stored file, to which format. The text to convert is
// never in the request; the CLI re-reads the stored file and checks it against the manifest.
type artifactConvertRequest struct {
	Name   string `json:"name"`
	Format string `json:"format"`
}

// artifactConvert makes a derived Office file of a stored Markdown file (docs/design/office-export.md, section 3).
// The conversion adds the file to its own version and never overwrites; the audit line names the actor and both hashes.
func (h *debateHub) artifactConvert(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	thread := r.PathValue("thread")
	n, valid := artifactVersionNumber(r.PathValue("version"))
	if !threadPattern.MatchString(thread) || !valid {
		errJSON(w, http.StatusBadRequest, "invalid thread or version")
		return
	}
	var req artifactConvertRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<10))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		errJSON(w, http.StatusBadRequest, "request body must be {\"name\": \"file.md\"}")
		return
	}
	if req.Format == "" {
		req.Format = "docx"
	}
	if !artifactConvertFormats[req.Format] {
		errJSON(w, http.StatusBadRequest, "format must be docx or pptx")
		return
	}
	if !artifactNamePattern.MatchString(req.Name) || strings.ToLower(filepath.Ext(req.Name)) != ".md" {
		errJSON(w, http.StatusBadRequest, "name must be a stored Markdown (.md) file")
		return
	}
	project, tdir, found := h.artifactThreadDir(thread)
	if !found {
		errJSON(w, http.StatusNotFound, "artifacts not found")
		return
	}
	if _, found := readArtifactManifest(tdir, n); !found {
		errJSON(w, http.StatusNotFound, "version not found")
		return
	}
	version := "v" + strconv.Itoa(n)
	command := "artifact convert " + req.Name + "@" + version + " -> " + req.Format
	code, out, errOut, err := h.runCLI(h.cfg.actionTimeout(), nil, "--artifact-convert", req.Name, "--project", project,
		"--thread", thread, "--artifact-version", "artifact@"+version, "--artifact-format", req.Format)
	var result struct {
		Source       string `json:"source"`
		SourceSHA256 string `json:"source_sha256"`
		ResultSHA256 string `json:"result_sha256"`
	}
	if code == 0 && err == nil {
		_ = json.Unmarshal(out, &result)
	}
	status, detail := "ok", "source_sha256="+result.SourceSHA256+" result_sha256="+result.ResultSHA256
	if err != nil || code != 0 {
		status, detail = "error", joinErr("exit "+strconv.Itoa(code), tailString(strings.TrimSpace(errOut+string(out)), 300))
		if err != nil {
			detail = err.Error()
		}
	}
	recordAudit(r, "POST /api/threads/{thread}/artifacts/{version}/convert", []string{project}, command, status, detail,
		map[string]int{"convert": code}, start)
	h.respondConfig(w, code, out, errOut, err)
}

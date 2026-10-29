package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

const artThread = "thread_20261018_090000_abc123"

type artFixture struct {
	th   *testHub
	root string
}

// newArtifactHub isolates both the data dir of the hub and the audit log (GUARDIAN_DATA_DIR).
func newArtifactHub(t *testing.T) *artFixture {
	t.Helper()
	t.Setenv("GUARDIAN_DATA_DIR", t.TempDir())
	th := newTestHub(t, nil)
	return &artFixture{th: th, root: filepath.Join(th.dir, "data", "design-artifacts")}
}

func sha(data string) string {
	sum := sha256.Sum256([]byte(data))
	return hex.EncodeToString(sum[:])
}

// version writes vN with the given files (name -> content) the way artifact_ops.py does.
func (f *artFixture) version(t *testing.T, project, thread string, n int, files map[string]string) string {
	t.Helper()
	vdir := filepath.Join(f.root, project, thread, "v"+strconv.Itoa(n))
	if err := os.MkdirAll(vdir, 0o700); err != nil {
		t.Fatal(err)
	}
	entries := []map[string]interface{}{}
	for name, content := range files {
		writeF(t, filepath.Join(vdir, name), content)
		entries = append(entries, map[string]interface{}{
			"path": name, "sha256": sha(content), "bytes": len(content), "mime": "text/html", "kind": "markdown",
			"stage": "final", "agent": "design_expert", "title": "T " + name,
		})
	}
	manifest := map[string]interface{}{
		"manifest_version": 1, "version": n, "project": project, "thread": thread,
		"created_at": "2026-10-18T09:00:00Z", "parent": nil, "written": "a.md", "files": entries,
	}
	raw, _ := json.Marshal(manifest)
	writeF(t, filepath.Join(vdir, "manifest.json"), string(raw))
	return vdir
}

func (f *artFixture) get(path string) *httptest.ResponseRecorder {
	return f.th.call("GET", path, "")
}

func artPath(version, rest string) string {
	return "/api/threads/" + artThread + "/artifacts/" + version + rest
}

func TestArtifactFileServedWithFixedHeaders(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "# Title\n", "d.svg": "<svg/>", "g.mmd": "graph TD; A-->B"})
	for name, want := range map[string]string{"a.md": "text/markdown; charset=utf-8", "d.svg": "image/svg+xml", "g.mmd": "text/plain; charset=utf-8"} {
		rec := f.get(artPath("v1", "/files/"+name))
		if rec.Code != 200 {
			t.Fatalf("%s: %d %s", name, rec.Code, rec.Body)
		}
		h := rec.Header()
		if h.Get("Content-Type") != want {
			t.Errorf("%s content type %q", name, h.Get("Content-Type"))
		}
		if !strings.HasPrefix(h.Get("Content-Security-Policy"), "sandbox;") || h.Get("X-Content-Type-Options") != "nosniff" ||
			h.Get("Referrer-Policy") != "no-referrer" || h.Get("Cache-Control") != "private, no-cache" {
			t.Errorf("%s headers: %v", name, h)
		}
		if h.Get("Content-Disposition") != "" {
			t.Errorf("%s: no attachment without ?download=1", name)
		}
	}
	rec := f.get(artPath("v1", "/files/a.md?download=1"))
	if cd := rec.Header().Get("Content-Disposition"); cd != `attachment; filename=a.md` {
		t.Errorf("disposition %q", cd)
	}
	if rec.Header().Get("ETag") != `"`+sha("# Title\n")+`"` {
		t.Errorf("etag %q", rec.Header().Get("ETag"))
	}
}

func TestArtifactConditionalAndRange(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "0123456789"})
	req := httptest.NewRequest("GET", artPath("v1", "/files/a.md"), nil)
	req.Header.Set("Range", "bytes=2-4")
	rec := httptest.NewRecorder()
	f.th.h.ServeHTTP(rec, req)
	if rec.Code != http.StatusPartialContent || rec.Body.String() != "234" {
		t.Fatalf("range: %d %q", rec.Code, rec.Body)
	}
	req = httptest.NewRequest("GET", artPath("v1", "/files/a.md"), nil)
	req.Header.Set("If-None-Match", `"`+sha("0123456789")+`"`)
	rec = httptest.NewRecorder()
	f.th.h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotModified {
		t.Fatalf("conditional: %d", rec.Code)
	}
}

func TestArtifactOnlyListedServedFilesAreReadable(t *testing.T) {
	f := newArtifactHub(t)
	vdir := f.version(t, "arch", artThread, 1, map[string]string{"a.md": "x"})
	writeF(t, filepath.Join(vdir, "stray.md"), "not in the manifest")
	writeF(t, filepath.Join(vdir, "script.js"), "alert(1)")
	for _, name := range []string{"stray.md", "manifest.json", "script.js", "missing.md"} {
		if rec := f.get(artPath("v1", "/files/"+name)); rec.Code != http.StatusNotFound {
			t.Errorf("%s: %d, want 404", name, rec.Code)
		}
	}
	for _, name := range []string{"..%2Fmanifest.json", "A.md", "a.md%00", "-x.md"} {
		if rec := f.get(artPath("v1", "/files/"+name)); rec.Code != http.StatusBadRequest && rec.Code != http.StatusNotFound {
			t.Errorf("%s: %d, want 400/404", name, rec.Code)
		}
	}
}

func TestArtifactTamperedManifestCannotExposeOtherFiles(t *testing.T) {
	f := newArtifactHub(t)
	vdir := f.version(t, "arch", artThread, 1, map[string]string{"a.md": "x"})
	raw := `{"manifest_version":1,"version":1,"project":"arch","thread":"` + artThread + `","created_at":"2026-10-18T09:00:00Z",
"parent":null,"written":"a.md","files":[{"path":"manifest.json","sha256":"` + sha("{}") + `","bytes":2},
{"path":"../../x.md","sha256":"00","bytes":1},{"path":"a.md","sha256":"` + sha("x") + `","bytes":1}]}`
	writeF(t, filepath.Join(vdir, "manifest.json"), raw)
	rec := f.get(artPath("v1", ""))
	m := decode(t, rec)
	files := m["files"].([]interface{})
	if rec.Code != 200 || len(files) != 1 || files[0].(map[string]interface{})["path"] != "a.md" {
		t.Fatalf("manifest filtered to served names: %d %v", rec.Code, m)
	}
	if rec := f.get(artPath("v1", "/files/manifest.json")); rec.Code != http.StatusNotFound {
		t.Errorf("manifest.json: %d", rec.Code)
	}
}

func TestArtifactContentMustMatchManifest(t *testing.T) {
	f := newArtifactHub(t)
	vdir := f.version(t, "arch", artThread, 1, map[string]string{"a.md": "original"})
	writeF(t, filepath.Join(vdir, "a.md"), "tampered!")
	if rec := f.get(artPath("v1", "/files/a.md")); rec.Code != http.StatusConflict {
		t.Fatalf("hash mismatch: %d, want 409", rec.Code)
	}
	if rec := f.get(artPath("v1", "/bundle.zip")); rec.Code != http.StatusConflict {
		t.Fatalf("bundle with a bad file: %d, want 409", rec.Code)
	}
}

func TestArtifactVersionRules(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "one"})
	f.version(t, "arch", artThread, 2, map[string]string{"a.md": "two"})
	tdir := filepath.Join(f.root, "arch", artThread)
	os.MkdirAll(filepath.Join(tdir, ".tmp-v3abc"), 0o700) // crashed write: no manifest
	os.MkdirAll(filepath.Join(tdir, "v4"), 0o700)         // no manifest: not complete
	rec := f.get("/api/threads/" + artThread + "/artifacts")
	vs := decode(t, rec)["versions"].([]interface{})
	if rec.Code != 200 || len(vs) != 2 {
		t.Fatalf("versions: %d %v", rec.Code, vs)
	}
	for v, want := range map[string]int{"v0": 400, "v01": 400, "v1x": 400, "x1": 400, "v3": 404, "v4": 404, "v99": 404, "v2": 200} {
		if got := f.get(artPath(v, "")).Code; got != want {
			t.Errorf("%s: %d, want %d", v, got, want)
		}
	}
}

func TestArtifactExpiredVersionIsATombstone(t *testing.T) {
	f := newArtifactHub(t)
	vdir := f.version(t, "arch", artThread, 1, map[string]string{"a.md": "old"})
	f.version(t, "arch", artThread, 2, map[string]string{"a.md": "new"})
	raw := `{"manifest_version":1,"version":1,"project":"arch","thread":"` + artThread + `","created_at":"2026-01-01T00:00:00Z",
"parent":null,"written":"a.md","expired_at":"2026-10-18T00:00:00Z","files":[{"path":"a.md","sha256":"` + sha("old") + `","bytes":3,"expired":true}]}`
	writeF(t, filepath.Join(vdir, "manifest.json"), raw)
	os.Remove(filepath.Join(vdir, "a.md"))
	vs := decode(t, f.get("/api/threads/"+artThread+"/artifacts"))["versions"].([]interface{})
	first := vs[0].(map[string]interface{})
	if len(vs) != 2 || first["expired_at"] != "2026-10-18T00:00:00Z" || first["files"].(float64) != 0 {
		t.Fatalf("tombstone listing: %v", vs)
	}
	if rec := f.get(artPath("v1", "/files/a.md")); rec.Code != http.StatusNotFound {
		t.Errorf("expired file: %d", rec.Code)
	}
	if rec := f.get(artPath("v1", "/bundle.zip")); rec.Code != http.StatusNotFound {
		t.Errorf("expired bundle: %d", rec.Code)
	}
	if rec := f.get(artPath("v2", "/files/a.md")); rec.Code != 200 || rec.Body.String() != "new" {
		t.Errorf("latest: %d", rec.Code)
	}
}

func TestArtifactSymlinksAreRefusedAtEveryLevel(t *testing.T) {
	// decoy is a complete, valid tree outside the data dir with the same content, so anything that follows a link
	// would answer 200; only the symlink checks can produce the refusals below.
	cases := map[string]func(t *testing.T, f *artFixture, decoy string){
		"file": func(t *testing.T, f *artFixture, decoy string) {
			vdir := f.version(t, "arch", artThread, 1, map[string]string{"a.md": "x"})
			os.Remove(filepath.Join(vdir, "a.md"))
			os.Symlink(filepath.Join(decoy, "arch", artThread, "v1", "a.md"), filepath.Join(vdir, "a.md"))
		},
		"manifest": func(t *testing.T, f *artFixture, decoy string) {
			vdir := f.version(t, "arch", artThread, 1, map[string]string{"a.md": "x"})
			os.Remove(filepath.Join(vdir, "manifest.json"))
			os.Symlink(filepath.Join(decoy, "arch", artThread, "v1", "manifest.json"), filepath.Join(vdir, "manifest.json"))
		},
		"version dir": func(t *testing.T, f *artFixture, decoy string) {
			os.MkdirAll(filepath.Join(f.root, "arch", artThread), 0o700)
			os.Symlink(filepath.Join(decoy, "arch", artThread, "v1"), filepath.Join(f.root, "arch", artThread, "v1"))
		},
		"thread dir": func(t *testing.T, f *artFixture, decoy string) {
			os.MkdirAll(filepath.Join(f.root, "arch"), 0o700)
			os.Symlink(filepath.Join(decoy, "arch", artThread), filepath.Join(f.root, "arch", artThread))
		},
		"project dir": func(t *testing.T, f *artFixture, decoy string) {
			os.MkdirAll(f.root, 0o700)
			os.Symlink(filepath.Join(decoy, "arch"), filepath.Join(f.root, "arch"))
		},
		"root": func(t *testing.T, f *artFixture, decoy string) {
			os.Symlink(decoy, f.root)
		},
	}
	for name, setup := range cases {
		t.Run(name, func(t *testing.T) {
			f := newArtifactHub(t)
			decoy := filepath.Join(t.TempDir(), "decoy")
			(&artFixture{th: f.th, root: decoy}).version(t, "arch", artThread, 1, map[string]string{"a.md": "x"})
			setup(t, f, decoy)
			for _, path := range []string{
				artPath("v1", "/files/a.md"), artPath("v1", ""), artPath("v1", "/bundle.zip"),
			} {
				if name == "file" && path == artPath("v1", "") {
					continue // the manifest is a real file there; only the linked artifact is refused
				}
				if rec := f.get(path); rec.Code != http.StatusNotFound {
					t.Errorf("%s: %s -> %d, want 404", name, path, rec.Code)
				}
			}
			if rec := f.get("/api/threads/" + artThread + "/artifacts"); name != "file" && rec.Code != http.StatusNotFound {
				t.Errorf("%s: version list -> %d, want 404", name, rec.Code)
			}
			if rec := f.get("/api/projects/arch/artifacts"); name != "file" && len(decode(t, rec)["threads"].([]interface{})) != 0 {
				t.Errorf("%s: project listing followed a symlink: %s", name, rec.Body)
			}
		})
	}
}

func TestArtifactBundleMatchesManifest(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "alpha", "d.svg": "<svg/>"})
	rec := f.get(artPath("v1", "/bundle.zip"))
	if rec.Code != 200 || rec.Header().Get("Content-Type") != "application/zip" {
		t.Fatalf("bundle: %d %v", rec.Code, rec.Header())
	}
	if !strings.Contains(rec.Header().Get("Content-Disposition"), artThread+"-v1.zip") {
		t.Errorf("name: %q", rec.Header().Get("Content-Disposition"))
	}
	zr, err := zip.NewReader(bytes.NewReader(rec.Body.Bytes()), int64(rec.Body.Len()))
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, zf := range zr.File {
		rc, _ := zf.Open()
		b, _ := io.ReadAll(rc)
		rc.Close()
		got[zf.Name] = string(b)
	}
	if got["a.md"] != "alpha" || got["d.svg"] != "<svg/>" || len(got) != 3 {
		t.Fatalf("members: %v", got)
	}
	var m artifactManifest
	if err := json.Unmarshal([]byte(got["manifest.json"]), &m); err != nil {
		t.Fatal(err)
	}
	for _, af := range m.Files {
		if sha(got[af.Path]) != af.SHA256 {
			t.Errorf("%s: sha256 differs from the manifest", af.Path)
		}
	}
}

func TestArtifactProjectListNewestFirst(t *testing.T) {
	f := newArtifactHub(t)
	other := "thread_20261017_100000_zzz999"
	f.version(t, "arch", other, 1, map[string]string{"a.md": "x"})
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "x"})
	f.version(t, "arch", artThread, 2, map[string]string{"a.md": "yy"})
	os.MkdirAll(filepath.Join(f.root, "arch", "not-a-thread"), 0o700)
	rec := f.get("/api/projects/arch/artifacts")
	threads := decode(t, rec)["threads"].([]interface{})
	if rec.Code != 200 || len(threads) != 2 {
		t.Fatalf("threads: %d %v", rec.Code, threads)
	}
	for _, raw := range threads {
		th := raw.(map[string]interface{})
		if th["thread_id"] == artThread && (th["versions"].(float64) != 2 || th["latest"].(map[string]interface{})["version"].(float64) != 2) {
			t.Errorf("latest of %s: %v", artThread, th)
		}
	}
	if rec := f.get("/api/projects/none/artifacts"); rec.Code != 200 || len(decode(t, rec)["threads"].([]interface{})) != 0 {
		t.Errorf("unknown project is an empty list: %d %s", rec.Code, rec.Body)
	}
	if rec := f.get("/api/projects/BAD/artifacts"); rec.Code != 400 {
		t.Errorf("invalid id: %d", rec.Code)
	}
}

func TestArtifactRoutesNeedAuthAndIgnoreQueryToken(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "alpha"})
	chain := withSecurity(withAuth(f.th.h, testToken), securityConfig{allowedHosts: map[string]bool{"example.com": true}})
	paths := []string{
		"/api/projects/arch/artifacts", "/api/threads/" + artThread + "/artifacts", artPath("v1", ""),
		artPath("v1", "/files/a.md"), artPath("v1", "/bundle.zip"),
	}
	bearer := func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+testToken) }
	for _, p := range paths {
		if rec := do(chain, "GET", p, "", nil); rec.Code != http.StatusUnauthorized {
			t.Errorf("%s without credentials: %d", p, rec.Code)
		}
		sep := "?"
		if strings.Contains(p, "?") {
			sep = "&"
		}
		if rec := do(chain, "GET", p+sep+"access_token="+testToken, "", nil); rec.Code != http.StatusUnauthorized {
			t.Errorf("%s with a query token: %d, want 401", p, rec.Code)
		}
		if rec := do(chain, "GET", p, "", bearer); rec.Code != 200 {
			t.Errorf("%s with Bearer: %d", p, rec.Code)
		}
	}
	rec := do(chain, "GET", artPath("v1", "/files/a.md"), "", func(r *http.Request) {
		bearer(r)
		r.Header.Set("Range", "bytes=0-1")
	})
	if rec.Code != http.StatusPartialContent || rec.Body.String() != "al" {
		t.Errorf("Bearer + Range: %d %q", rec.Code, rec.Body)
	}
}

func TestArtifactViewsAreAuditedByNameOnly(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "SECRET-BODY-TEXT"})
	f.get(artPath("v1", "/files/a.md"))
	f.get(artPath("v1", "/bundle.zip"))
	f.get(artPath("v1", "/files/missing.md")) // refused: not audited
	entries, err := readAuditTail(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("audit entries: %+v", entries)
	}
	if entries[1].Command != "artifact a.md@v1" || entries[1].Targets[0] != "arch" || entries[0].Command != "artifact bundle @v1" {
		t.Errorf("entries: %+v", entries)
	}
	for _, e := range entries {
		if strings.Contains(e.Command+e.Detail, "SECRET-BODY-TEXT") {
			t.Errorf("content leaked into the audit log: %+v", e)
		}
	}
}

func TestArtifactConvertCallsTheCLIAndAuditsBothHashes(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "alpha"})
	rec := f.th.call("POST", artPath("v1", "/convert"), `{"name":"a.md"}`)
	if rec.Code != 200 {
		t.Fatalf("convert: %d %s", rec.Code, rec.Body)
	}
	if got := decode(t, rec); got["result_sha256"] != "bbbb" || got["source"] != "a.md" {
		t.Errorf("response: %v", got)
	}
	args, _ := os.ReadFile(filepath.Join(f.th.dir, "last-args.txt"))
	want := []string{"--artifact-convert", "a.md", "--project", "arch", "--thread", artThread, "--artifact-version", "artifact@v1", "--artifact-format", "docx"}
	if got := strings.Fields(string(args)); strings.Join(got, " ") != strings.Join(want, " ") {
		t.Errorf("CLI arguments: %v", got)
	}
	entries, err := readAuditTail(5)
	if err != nil || len(entries) != 1 {
		t.Fatalf("audit: %v %+v", err, entries)
	}
	e := entries[0]
	if e.Command != "artifact convert a.md@v1 -> docx" || e.Targets[0] != "arch" || e.Status != "ok" ||
		e.Detail != "source_sha256=aaaa result_sha256=bbbb" || e.Actor == "" {
		t.Errorf("audit entry: %+v", e)
	}
}

func TestArtifactConvertAcceptsPptx(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "alpha"})
	rec := f.th.call("POST", artPath("v1", "/convert"), `{"name":"a.md","format":"pptx"}`)
	if rec.Code != 200 {
		t.Fatalf("convert: %d %s", rec.Code, rec.Body)
	}
	args, _ := os.ReadFile(filepath.Join(f.th.dir, "last-args.txt"))
	if got := strings.Fields(string(args)); got[len(got)-1] != "pptx" || got[len(got)-2] != "--artifact-format" {
		t.Errorf("CLI arguments: %v", got)
	}
	entries, err := readAuditTail(5)
	if err != nil || len(entries) != 1 || entries[0].Command != "artifact convert a.md@v1 -> pptx" {
		t.Errorf("audit: %v %+v", err, entries)
	}
}

func TestArtifactConvertRefusals(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "alpha"})
	cases := []struct {
		name, path, body string
		want             int
	}{
		{"not json", artPath("v1", "/convert"), `nope`, 400},
		{"unknown field", artPath("v1", "/convert"), `{"name":"a.md","text":"# x"}`, 400},
		{"svg", artPath("v1", "/convert"), `{"name":"a.svg"}`, 400},
		{"already docx", artPath("v1", "/convert"), `{"name":"a.docx"}`, 400},
		{"traversal", artPath("v1", "/convert"), `{"name":"../a.md"}`, 400},
		{"upper case", artPath("v1", "/convert"), `{"name":"A.md"}`, 400},
		{"format", artPath("v1", "/convert"), `{"name":"a.md","format":"xlsx"}`, 400},
		{"already pptx", artPath("v1", "/convert"), `{"name":"a.pptx"}`, 400},
		{"bad version", artPath("v01", "/convert"), `{"name":"a.md"}`, 400},
		{"unknown version", artPath("v9", "/convert"), `{"name":"a.md"}`, 404},
		{"unknown thread", "/api/threads/thread_20261018_090000_zzzzzz/artifacts/v1/convert", `{"name":"a.md"}`, 404},
		{"cli invalid", artPath("v1", "/convert"), `{"name":"invalid.md"}`, 400},
		{"cli exists", artPath("v1", "/convert"), `{"name":"dup.md"}`, 409},
		{"cli crash", artPath("v1", "/convert"), `{"name":"crash.md"}`, 500},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if rec := f.th.call("POST", c.path, c.body); rec.Code != c.want {
				t.Errorf("%d, want %d: %s", rec.Code, c.want, rec.Body)
			}
		})
	}
	if rec := f.th.call("GET", artPath("v1", "/convert"), ""); rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET convert: %d", rec.Code)
	}
}

func TestArtifactConvertNeedsCredentials(t *testing.T) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "alpha"})
	chain := withSecurity(withAuth(f.th.h, testToken), securityConfig{allowedHosts: map[string]bool{"example.com": true}})
	json := func(r *http.Request) { r.Header.Set("Content-Type", "application/json") }
	if rec := do(chain, "POST", artPath("v1", "/convert"), `{"name":"a.md"}`, json); rec.Code != http.StatusUnauthorized {
		t.Errorf("without credentials: %d", rec.Code)
	}
	if rec := do(chain, "POST", artPath("v1", "/convert?access_token="+testToken), `{"name":"a.md"}`, json); rec.Code != http.StatusUnauthorized {
		t.Errorf("with a query token: %d", rec.Code)
	}
	if rec := do(chain, "POST", artPath("v1", "/convert"), `{"name":"a.md"}`, func(r *http.Request) {
		json(r)
		r.Header.Set("Authorization", "Bearer "+testToken)
	}); rec.Code != 200 {
		t.Errorf("with Bearer: %d %s", rec.Code, rec.Body)
	}
}

func TestDerivedOfficeFilesAreAlwaysAttachmentsWithTheirFixedType(t *testing.T) {
	for name, mime := range map[string]string{
		"a.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
		"a.pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
	} {
		t.Run(name, func(t *testing.T) { checkDerivedAttachment(t, name, mime) })
	}
}

func checkDerivedAttachment(t *testing.T, name, mime string) {
	f := newArtifactHub(t)
	f.version(t, "arch", artThread, 1, map[string]string{"a.md": "alpha", name: "PK-bytes"})
	for _, query := range []string{"", "?download=1", "?download=0"} {
		rec := f.get(artPath("v1", "/files/"+name+query))
		if rec.Code != 200 {
			t.Fatalf("office%s: %d", query, rec.Code)
		}
		if got := rec.Header().Get("Content-Disposition"); !strings.HasPrefix(got, "attachment") {
			t.Errorf("office%s Content-Disposition %q", query, got)
		}
		if got := rec.Header().Get("Content-Type"); got != mime {
			t.Errorf("office%s Content-Type %q", query, got)
		}
		if rec.Header().Get("X-Content-Type-Options") != "nosniff" || !strings.Contains(rec.Header().Get("Content-Security-Policy"), "sandbox") {
			t.Errorf("office%s headers: %v", query, rec.Header())
		}
	}
	if rec := f.get(artPath("v1", "/files/a.md")); rec.Header().Get("Content-Disposition") != "" {
		t.Errorf("markdown stays inline without ?download=1")
	}
	m := decode(t, f.get(artPath("v1", "")))
	if len(m["files"].([]interface{})) != 2 {
		t.Errorf("manifest lists the derived file: %v", m["files"])
	}
}

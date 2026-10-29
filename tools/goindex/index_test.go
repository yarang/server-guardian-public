package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fixture writes files (slash path -> content) below a fresh directory with a go.mod and returns the directory.
func fixture(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	files["go.mod"] = "module example.com/m\n\ngo 1.22\n"
	for name, content := range files {
		path := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// indexDir indexes the module at the fixture root; every Go file is readable except those in hidden.
func indexDir(t *testing.T, dir string, hidden ...string) *Facts {
	t.Helper()
	readable := map[string]bool{}
	_ = filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() && strings.HasSuffix(path, ".go") {
			rel, _ := filepath.Rel(dir, path)
			readable[filepath.ToSlash(rel)] = true
		}
		return nil
	})
	for _, h := range hidden {
		delete(readable, h)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	facts, err := Index(ctx, Config{Root: dir, Modules: []string{"."}, Readable: readable, SHA: "abc"})
	if err != nil {
		t.Fatalf("Index: %v", err)
	}
	return facts
}

func (f *Facts) def(t *testing.T, qualified, path string) DefFact {
	t.Helper()
	for _, d := range f.Defs {
		if d.Qualified == qualified && (path == "" || d.Path == path) && !d.External {
			return d
		}
	}
	t.Fatalf("no definition %s in %q; have %v", qualified, path, f.Defs)
	return DefFact{}
}

func (f *Facts) refsTo(id int) []RefFact {
	var out []RefFact
	for _, r := range f.Refs {
		if r.Def == id {
			out = append(out, r)
		}
	}
	return out
}

func TestSameNameInTwoPackagesOnlyCalledOneHasReferences(t *testing.T) {
	dir := fixture(t, map[string]string{
		"a/a.go":   "package a\n\nfunc Handle() {}\n",
		"b/b.go":   "package b\n\nfunc Handle() {}\n",
		"main.go":  "package main\n\nimport \"example.com/m/a\"\n\nfunc main() {\n\ta.Handle()\n}\n",
		"other.go": "package main\n\nimport _ \"example.com/m/b\"\n",
	})
	facts := indexDir(t, dir)
	a, b := facts.def(t, "Handle", "a/a.go"), facts.def(t, "Handle", "b/b.go")
	if a.Pkg != "example.com/m/a" || b.Pkg != "example.com/m/b" {
		t.Fatalf("packages: %q %q", a.Pkg, b.Pkg)
	}
	refs := facts.refsTo(a.ID)
	if len(refs) != 1 || !refs[0].Call || refs[0].Path != "main.go" || refs[0].Enclosing != "main" || refs[0].Line != 6 {
		t.Fatalf("refs to a.Handle: %+v", refs)
	}
	if got := facts.refsTo(b.ID); len(got) != 0 {
		t.Fatalf("b.Handle is never used but has refs: %+v", got)
	}
}

func TestMethodAndFunctionWithTheSameName(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": `package m

type S struct{}

func Run() {}

func (s *S) Run() {}

func use(s *S) {
	s.Run()
}

func other() {
	Run()
}
`})
	facts := indexDir(t, dir)
	method, fn := facts.def(t, "S.Run", ""), facts.def(t, "Run", "x.go")
	if method.Kind != "method" || fn.Kind != "function" || method.ID == fn.ID {
		t.Fatalf("kinds: %+v %+v", method, fn)
	}
	if method.Start != 7 || method.End != 7 {
		t.Fatalf("method span %d-%d", method.Start, method.End)
	}
	mrefs, frefs := facts.refsTo(method.ID), facts.refsTo(fn.ID)
	if len(mrefs) != 1 || mrefs[0].Enclosing != "use" || !mrefs[0].Call {
		t.Fatalf("method refs: %+v", mrefs)
	}
	if len(frefs) != 1 || frefs[0].Enclosing != "other" {
		t.Fatalf("function refs: %+v", frefs)
	}
}

func TestInterfaceCallPointsAtTheInterfaceMethod(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": `package m

type Doer interface {
	Do()
}

type Impl struct{}

func (Impl) Do() {}

func use(d Doer) {
	d.Do()
}

func direct(i Impl) {
	i.Do()
}
`})
	facts := indexDir(t, dir)
	iface, impl := facts.def(t, "Doer.Do", ""), facts.def(t, "Impl.Do", "")
	if iface.Kind != "method" || iface.Start != 4 {
		t.Fatalf("interface method: %+v", iface)
	}
	viaInterface, viaImpl := facts.refsTo(iface.ID), facts.refsTo(impl.ID)
	if len(viaInterface) != 1 || viaInterface[0].Enclosing != "use" {
		t.Fatalf("interface method refs: %+v", viaInterface)
	}
	if len(viaImpl) != 1 || viaImpl[0].Enclosing != "direct" {
		t.Fatalf("implementation refs: %+v", viaImpl)
	}
}

func TestTestFilesAreMarkedAndTheirReferencesKept(t *testing.T) {
	dir := fixture(t, map[string]string{
		"x.go":      "package m\n\nfunc Helper() int { return 1 }\n",
		"x_test.go": "package m\n\nimport \"testing\"\n\nfunc helperForTest() {}\n\nfunc TestX(t *testing.T) {\n\tHelper()\n\thelperForTest()\n}\n",
		"y_test.go": "package m_test\n\nimport (\n\t\"testing\"\n\n\t\"example.com/m\"\n)\n\nfunc TestY(t *testing.T) {\n\tm.Helper()\n}\n",
	})
	facts := indexDir(t, dir)
	helper := facts.def(t, "Helper", "x.go")
	if helper.Test {
		t.Fatal("Helper is not a test definition")
	}
	if !facts.def(t, "helperForTest", "x_test.go").Test {
		t.Fatal("helperForTest lives in a test file")
	}
	refs := facts.refsTo(helper.ID)
	if len(refs) != 2 {
		t.Fatalf("refs to Helper: %+v", refs)
	}
	for _, r := range refs {
		if !r.Test || !r.Call {
			t.Fatalf("test call expected: %+v", r)
		}
	}
	// A regular file appears once even though the package is also checked together with its tests.
	n := 0
	for _, d := range facts.Defs {
		if d.Qualified == "Helper" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("Helper defined %d times", n)
	}
	var hasTest bool
	for _, p := range facts.Packages {
		hasTest = hasTest || p.Test
	}
	if !hasTest {
		t.Fatalf("no test package: %+v", facts.Packages)
	}
}

func TestTypeErrorGivesPartialFactsAndUnresolved(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": `package m

func Good() {}

func caller() {
	Good()
}

func Broken() {
	missing.Call()
	Good()
}
`})
	facts := indexDir(t, dir)
	good := facts.def(t, "Good", "")
	if got := len(facts.refsTo(good.ID)); got != 2 {
		t.Fatalf("resolved references despite the error: %d", got)
	}
	var found bool
	for _, u := range facts.Unresolved {
		found = found || (u.Name == "missing" && u.Line == 10 && u.Path == "x.go")
	}
	if !found {
		t.Fatalf("unresolved: %+v", facts.Unresolved)
	}
	var withErrors int
	for _, p := range facts.Packages {
		if len(p.Errors) > 0 {
			withErrors++
			if !strings.HasPrefix(p.Errors[0], "x.go:") {
				t.Fatalf("package error: %v", p.Errors)
			}
		}
	}
	if withErrors == 0 {
		t.Fatalf("package errors missing: %+v", facts.Packages)
	}
}

func TestReadableFilterDropsUnreadableFilesAndKeepsNoSourceText(t *testing.T) {
	dir := fixture(t, map[string]string{
		"open.go": "package m\n\n// COMMENT_TEXT_MARKER\nfunc Open() string {\n\treturn \"LITERAL_TEXT_MARKER\" + hidden()\n}\n",
		"hide.go": "package m\n\nfunc hidden() string {\n\tOpen()\n\treturn \"x\"\n}\n",
	})
	facts := indexDir(t, dir, "hide.go")
	for _, d := range facts.Defs {
		if d.Path == "hide.go" || d.Qualified == "hidden" {
			t.Fatalf("unreadable file leaked into definitions: %+v", d)
		}
	}
	for _, r := range facts.Refs {
		if r.Path == "hide.go" {
			t.Fatalf("unreadable file leaked into references: %+v", r)
		}
	}
	for _, d := range facts.Defs {
		if d.External {
			t.Fatalf("a call into an unreadable file of the module must not become an external callee: %+v", d)
		}
	}
	raw, err := json.Marshal(facts)
	if err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"COMMENT_TEXT_MARKER", "LITERAL_TEXT_MARKER", "return"} {
		if strings.Contains(string(raw), text) {
			t.Fatalf("source text %q in the facts", text)
		}
	}
}

func TestExternalCalleesAreKeptOncePerFunction(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": `package m

import "strings"

func Shout(s string) string {
	var b strings.Builder
	b.WriteString(strings.ToUpper(s))
	b.WriteString(strings.ToUpper(s))
	return b.String()
}
`})
	facts := indexDir(t, dir)
	byName := map[string]DefFact{}
	for _, d := range facts.Defs {
		if d.External {
			byName[d.Pkg+"."+d.Qualified] = d
		}
	}
	for _, want := range []string{"strings.ToUpper", "strings.Builder.WriteString", "strings.Builder.String"} {
		if _, ok := byName[want]; !ok {
			t.Fatalf("external callee %s missing in %v", want, byName)
		}
	}
	upper := byName["strings.ToUpper"]
	if upper.Kind != "function" || upper.Path != "" {
		t.Fatalf("external definition: %+v", upper)
	}
	if got := len(facts.refsTo(upper.ID)); got != 1 {
		t.Fatalf("ToUpper calls are kept once per function, got %d", got)
	}
}

func TestEnclosingFunctionOfNestedAndPackageLevelUses(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": `package m

func Target() int { return 1 }

var initial = Target()

type T struct{}

func (t T) Method() func() int {
	return func() int { return Target() }
}
`})
	facts := indexDir(t, dir)
	enclosing := map[string]bool{}
	for _, r := range facts.refsTo(facts.def(t, "Target", "").ID) {
		enclosing[r.Enclosing] = true
	}
	if !enclosing[""] || !enclosing["T.Method"] || len(enclosing) != 2 {
		t.Fatalf("enclosing functions: %v", enclosing)
	}
}

func TestGroupedDeclarationsAndTypeKinds(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": `package m

const (
	A = 1
	B = 2
)

type (
	Shape struct{ X int }
	Name  string
)

var Count int
`})
	facts := indexDir(t, dir)
	a := facts.def(t, "A", "")
	if a.Kind != "const" || a.Start != 4 || a.End != 4 {
		t.Fatalf("grouped constant: %+v", a)
	}
	if shape := facts.def(t, "Shape", ""); shape.Kind != "struct" || shape.Start != 9 {
		t.Fatalf("struct: %+v", shape)
	}
	if name := facts.def(t, "Name", ""); name.Kind != "type" {
		t.Fatalf("type: %+v", name)
	}
	if count := facts.def(t, "Count", ""); count.Kind != "var" || count.Start != 13 || !count.Exported {
		t.Fatalf("var: %+v", count)
	}
}

func TestModuleThatCannotBeListedIsReported(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte("module broken\n\ngo 1.22\n\nrequire example.com/absent v1.0.0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "x.go"), []byte("package broken\n\nimport _ \"example.com/absent\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	facts, err := Index(context.Background(), Config{Root: dir, Modules: []string{"."}, Readable: map[string]bool{"x.go": true}})
	if err == nil {
		// -e keeps the package: the missing dependency is a package error, not a module failure.
		if len(facts.Packages) == 0 {
			t.Fatalf("expected the package with its error: %+v", facts)
		}
		return
	}
	if facts == nil || len(facts.ModuleErrors) != 1 || facts.ModuleErrors[0].Dir != "." {
		t.Fatalf("module error expected: %v %+v", err, facts)
	}
}

func TestChildEnvForcesTheSafeSettings(t *testing.T) {
	env := childEnv([]string{"PATH=/bin", "CGO_ENABLED=1", "GOFLAGS=-mod=mod", "GOTOOLCHAIN=auto", "GOPROXY=https://x", "GOCACHE=/c"})
	got := map[string]string{}
	for _, kv := range env {
		name, value, _ := strings.Cut(kv, "=")
		if _, dup := got[name]; dup {
			t.Fatalf("%s set twice: %v", name, env)
		}
		got[name] = value
	}
	want := map[string]string{"CGO_ENABLED": "0", "GOFLAGS": "-mod=readonly", "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOWORK": "off", "GOCACHE": "/c", "PATH": "/bin"}
	for k, v := range want {
		if got[k] != v {
			t.Fatalf("%s = %q, want %q", k, got[k], v)
		}
	}
}

func TestBasePath(t *testing.T) {
	cases := map[string]string{"a/b": "a/b", "a/b [a/b.test]": "a/b", "a/b_test [a/b.test]": "a/b_test"}
	for in, want := range cases {
		if got := basePath(in); got != want {
			t.Fatalf("basePath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRunWritesFactsAndRejectsBadUsage(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": "package m\n\nfunc One() {}\n"})
	list := filepath.Join(t.TempDir(), "readable.txt")
	if err := os.WriteFile(list, []byte("x.go\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(t.TempDir(), "facts.json")
	var stderr strings.Builder
	if code := run([]string{"-root", dir, "-module", ".", "-readable", list, "-out", out, "-sha", "deadbeef"}, &stderr); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	var facts Facts
	if err := json.Unmarshal(data, &facts); err != nil {
		t.Fatal(err)
	}
	if facts.Version != Version || facts.SHA != "deadbeef" || !strings.HasPrefix(facts.Go, "go") || len(facts.Defs) != 1 || len(facts.Files) != 1 || facts.Files[0] != "x.go" {
		t.Fatalf("facts: %+v", facts)
	}
	if code := run([]string{"-root", dir}, &stderr); code != 2 {
		t.Fatalf("missing flags should exit 2, got %d", code)
	}
}

func TestTimeoutExitsWithStatusThree(t *testing.T) {
	dir := fixture(t, map[string]string{"x.go": "package m\n\nfunc One() {}\n"})
	list := filepath.Join(t.TempDir(), "readable.txt")
	if err := os.WriteFile(list, []byte("x.go\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(t.TempDir(), "facts.json")
	var stderr strings.Builder
	if code := run([]string{"-root", dir, "-module", ".", "-readable", list, "-out", out, "-timeout", "1ns"}, &stderr); code != 3 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	if _, err := os.Stat(out); err == nil {
		t.Fatal("no facts may be written after a timeout")
	}
}

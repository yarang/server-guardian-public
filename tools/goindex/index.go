package main

import (
	"context"
	"fmt"
	"go/ast"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const maxUnresolved = 5000

// Config describes one indexing run.
type Config struct {
	Root     string          // the worktree
	Modules  []string        // module directories relative to Root ("." for the root itself)
	Readable map[string]bool // repository-relative slash paths that may appear in the output
	SHA      string          // echoed into the output
	GoBin    string          // the go command (default "go")
}

// checkedPkg is one package of the main module that was type-checked from source.
type checkedPkg struct {
	listed    *listedPackage
	base      string
	module    string
	variant   bool // a test variant: only its _test.go files are reported
	files     []*ast.File
	filenames []string
	info      *types.Info
	hardErrs  []string // "path:line" of errors that leave identifiers unresolved
	allErrs   int
}

type indexer struct {
	cfg      Config
	g        *goRunner
	roots    []string // Root as given and with symlinks resolved
	fset     *token.FileSet
	parsed   map[string]*ast.File
	checked  []*checkedPkg
	ownPaths map[string]bool // import paths of the packages type-checked from source
	defKeys  []string        // position key of each collected definition, in collection order
	byKey    map[string]int  // position key -> definition id
	facts    *Facts
}

// Index runs go list and the type checker over the modules and returns the facts. A module that cannot be listed
// at all is recorded in ModuleErrors; the run fails only when no module could be indexed.
func Index(ctx context.Context, cfg Config) (*Facts, error) {
	root, err := filepath.Abs(cfg.Root)
	if err != nil {
		return nil, err
	}
	roots := []string{root}
	if resolved, err := filepath.EvalSymlinks(root); err == nil && resolved != root {
		roots = append(roots, resolved)
	}
	ix := &indexer{
		cfg: cfg, g: newGoRunner(cfg.GoBin), roots: roots, fset: token.NewFileSet(),
		parsed: map[string]*ast.File{}, ownPaths: map[string]bool{},
		facts: &Facts{Version: Version, SHA: cfg.SHA, Modules: []string{}, ModuleErrors: []ModuleError{},
			Packages: []PackageFact{}, Files: []string{}, Defs: []DefFact{}, Refs: []RefFact{}, Unresolved: []UnresolvedFact{}},
	}
	version, goos, goarch, err := ix.g.env3(ctx, root)
	if err != nil {
		return nil, err
	}
	ix.facts.Go, ix.facts.GOOS, ix.facts.GOARCH = version, goos, goarch
	for _, module := range cfg.Modules {
		module = strings.Trim(filepath.ToSlash(filepath.Clean(module)), "/")
		if module == "" {
			module = "."
		}
		if err := ix.indexModule(ctx, module); err != nil {
			if ctxErr := ctx.Err(); ctxErr != nil {
				return nil, ctxErr
			}
			ix.facts.ModuleErrors = append(ix.facts.ModuleErrors, ModuleError{Dir: module, Error: oneLine(err.Error(), 300)})
			continue
		}
		ix.facts.Modules = append(ix.facts.Modules, module)
	}
	ix.emit()
	sort.Strings(ix.facts.Modules)
	if len(ix.facts.Modules) == 0 {
		return ix.facts, fmt.Errorf("no module could be indexed")
	}
	return ix.facts, nil
}

func oneLine(s string, limit int) string {
	s = strings.Join(strings.Fields(s), " ")
	if len(s) > limit {
		s = s[:limit]
	}
	return s
}

// rel is the slash path of abs below the root, if it is below it.
func (ix *indexer) rel(abs string) (string, bool) {
	for _, root := range ix.roots {
		r, err := filepath.Rel(root, abs)
		if err != nil || r == ".." || strings.HasPrefix(r, ".."+string(filepath.Separator)) {
			continue
		}
		return filepath.ToSlash(r), true
	}
	return "", false
}

func (ix *indexer) underRoot(path string) bool {
	_, ok := ix.rel(path)
	return ok
}

// isOwn reports whether a listed package is source of the main module below the worktree. Generated test mains
// (their files live in the build cache) and dependencies are not.
func (ix *indexer) isOwn(p *listedPackage) bool {
	if p.Module == nil || !p.Module.Main || p.Dir == "" || !ix.underRoot(p.Dir) || len(p.GoFiles) == 0 {
		return false
	}
	for _, name := range p.GoFiles {
		if filepath.IsAbs(name) && !ix.underRoot(name) {
			return false
		}
	}
	return true
}

func (ix *indexer) parse(filename string) *ast.File {
	if file, ok := ix.parsed[filename]; ok {
		return file
	}
	// A file with syntax errors still yields a partial tree that the checker can use.
	file, _ := parser.ParseFile(ix.fset, filename, nil, parser.SkipObjectResolution)
	ix.parsed[filename] = file
	return file
}

// moduleImporter resolves imports of one package: packages of the main module come from this run's type-checking
// (so a package with errors still has its types), everything else from the export data go list produced.
type moduleImporter struct {
	ix      *indexer
	pkg     *listedPackage
	checked map[string]*types.Package
	gc      types.Importer
}

func (m *moduleImporter) Import(path string) (*types.Package, error) {
	if path == "unsafe" {
		return types.Unsafe, nil
	}
	id := path
	if mapped, ok := m.pkg.ImportMap[path]; ok {
		id = mapped
	}
	if p, ok := m.checked[id]; ok {
		return p, nil
	}
	if m.ix.ownPaths[basePath(id)] {
		return nil, fmt.Errorf("package %s was not type-checked (import cycle?)", id)
	}
	return m.gc.Import(basePath(id))
}

func (ix *indexer) indexModule(ctx context.Context, module string) error {
	dir := filepath.Join(ix.roots[0], filepath.FromSlash(module))
	pkgs, _, err := ix.g.list(ctx, dir)
	if err != nil {
		return err
	}
	exports := map[string]string{}
	for _, p := range pkgs {
		if p.Export != "" && p.ForTest == "" && !strings.Contains(p.ImportPath, " [") {
			exports[p.ImportPath] = p.Export
		}
	}
	lookup := func(path string) (io.ReadCloser, error) {
		file, ok := exports[path]
		if !ok {
			return nil, fmt.Errorf("no export data for %s", path)
		}
		return os.Open(file)
	}
	gc := importer.ForCompiler(ix.fset, "gc", lookup)

	own := make([]*listedPackage, 0, len(pkgs))
	for _, p := range pkgs {
		if ix.isOwn(p) {
			own = append(own, p)
			ix.ownPaths[basePath(p.ImportPath)] = true
		}
	}
	if len(own) == 0 {
		return fmt.Errorf("no Go packages found")
	}
	typed := map[string]*types.Package{}
	for _, p := range own {
		cp := &checkedPkg{listed: p, base: basePath(p.ImportPath), module: module, variant: p.ForTest != ""}
		for _, name := range p.GoFiles {
			filename := name
			if !filepath.IsAbs(filename) {
				filename = filepath.Join(p.Dir, name)
			}
			if file := ix.parse(filename); file != nil {
				cp.files = append(cp.files, file)
				cp.filenames = append(cp.filenames, filename)
			}
		}
		cp.info = &types.Info{Defs: map[*ast.Ident]types.Object{}, Uses: map[*ast.Ident]types.Object{}}
		conf := types.Config{
			Importer: &moduleImporter{ix: ix, pkg: p, checked: typed, gc: gc},
			Sizes:    types.SizesFor("gc", ix.facts.GOARCH),
			Error: func(err error) {
				cp.allErrs++
				terr, ok := err.(types.Error)
				if !ok || terr.Soft {
					return
				}
				if rel, ok := ix.rel(terr.Fset.Position(terr.Pos).Filename); ok && ix.cfg.Readable[rel] {
					cp.hardErrs = append(cp.hardErrs, fmt.Sprintf("%s:%d", rel, terr.Fset.Position(terr.Pos).Line))
				} else {
					cp.hardErrs = append(cp.hardErrs, "") // an error in a file that must not be named
				}
			},
		}
		if p.Module.GoVersion != "" {
			conf.GoVersion = "go" + p.Module.GoVersion
		}
		tpkg, _ := conf.Check(cp.base, ix.fset, cp.files, cp.info)
		typed[p.ImportPath] = tpkg
		ix.checked = append(ix.checked, cp)
	}
	return nil
}

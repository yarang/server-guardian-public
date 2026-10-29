package main

import (
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"sort"
	"strings"
)

const (
	kindFunction  = "function"
	kindMethod    = "method"
	kindStruct    = "struct"
	kindInterface = "interface"
	kindType      = "type"
	kindVar       = "var"
	kindConst     = "const"
)

// posKey identifies a declaration by where its name is written; objects of the same declaration seen through
// different type-checking runs (a package and its test variant) share it.
func (ix *indexer) posKey(pos token.Pos) string {
	p := ix.fset.Position(pos)
	return fmt.Sprintf("%s:%d:%d", p.Filename, p.Line, p.Column)
}

// source is where a file's facts go: its slash path, whether it is a test file, and whether it is reported at all.
func (ix *indexer) source(cp *checkedPkg, filename string) (path string, test, ok bool) {
	path, inRoot := ix.rel(filename)
	if !inRoot || !ix.cfg.Readable[path] {
		return "", false, false
	}
	test = strings.HasSuffix(path, "_test.go")
	// A test variant repeats the package's regular files; those are reported by the regular package.
	if cp.variant && !test {
		return "", false, false
	}
	return path, test, true
}

type declared struct {
	DefFact
	pos token.Pos
}

type externalRef struct {
	key string
	ref RefFact
}

// emit turns the type-checked packages into packages, definitions, references and unresolved identifiers.
func (ix *indexer) emit() {
	ix.emitFiles()
	ix.emitDefs()
	ix.emitRefs()
	ix.emitPackages()
}

// emitFiles lists the Go files that were type-checked and may be reported; a file that is not listed (another
// platform's, or excluded by build tags) has no facts, which tells the engine to keep its name-based results.
func (ix *indexer) emitFiles() {
	seen := map[string]bool{}
	for _, cp := range ix.checked {
		for i := range cp.files {
			if path, _, ok := ix.source(cp, cp.filenames[i]); ok && !seen[path] {
				seen[path] = true
				ix.facts.Files = append(ix.facts.Files, path)
			}
		}
	}
	sort.Strings(ix.facts.Files)
}

func (ix *indexer) emitDefs() {
	var defs []declared
	seen := map[string]bool{}
	for _, cp := range ix.checked {
		for i, file := range cp.files {
			path, test, ok := ix.source(cp, cp.filenames[i])
			if !ok {
				continue
			}
			for _, d := range ix.fileDefs(cp, file, path, test) {
				if key := ix.posKey(d.pos); !seen[key] {
					seen[key] = true
					ix.defKeys = append(ix.defKeys, key)
					defs = append(defs, d)
				}
			}
		}
	}
	order := make([]int, len(defs))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(i, j int) bool {
		a, b := defs[order[i]], defs[order[j]]
		if a.Path != b.Path {
			return a.Path < b.Path
		}
		if a.Start != b.Start {
			return a.Start < b.Start
		}
		return a.Qualified < b.Qualified
	})
	ix.byKey = map[string]int{}
	for n, i := range order {
		d := defs[i].DefFact
		d.ID = n + 1
		ix.byKey[ix.defKeys[i]] = d.ID
		ix.facts.Defs = append(ix.facts.Defs, d)
	}
}

// fileDefs lists the declarations of one file: functions, methods, types (and the methods of interface types),
// package-level variables and constants. Fields, parameters and locals are not definitions.
func (ix *indexer) fileDefs(cp *checkedPkg, file *ast.File, path string, test bool) []declared {
	var out []declared
	line := func(p token.Pos) int { return ix.fset.Position(p).Line }
	add := func(id *ast.Ident, qualified, kind string, start, end token.Pos) {
		if id == nil || id.Name == "_" || id.Name == "init" && kind == kindFunction {
			return
		}
		out = append(out, declared{pos: id.Pos(), DefFact: DefFact{
			Name: id.Name, Qualified: qualified, Kind: kind, Pkg: cp.base, Path: path,
			Start: line(start), End: line(end), Exported: ast.IsExported(id.Name), Test: test,
		}})
	}
	for _, decl := range file.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			if d.Recv == nil || len(d.Recv.List) == 0 {
				add(d.Name, d.Name.Name, kindFunction, d.Pos(), d.End())
			} else if recv := receiverName(d.Recv.List[0].Type); recv != "" {
				add(d.Name, recv+"."+d.Name.Name, kindMethod, d.Pos(), d.End())
			}
		case *ast.GenDecl:
			grouped := d.Lparen.IsValid()
			for _, spec := range d.Specs {
				start, end := d.Pos(), d.End()
				if grouped {
					start, end = spec.Pos(), spec.End()
				}
				switch s := spec.(type) {
				case *ast.TypeSpec:
					kind := kindType
					switch t := s.Type.(type) {
					case *ast.StructType:
						kind = kindStruct
					case *ast.InterfaceType:
						kind = kindInterface
						for _, m := range t.Methods.List {
							for _, name := range m.Names {
								add(name, s.Name.Name+"."+name.Name, kindMethod, m.Pos(), m.End())
							}
						}
					}
					add(s.Name, s.Name.Name, kind, start, end)
				case *ast.ValueSpec:
					kind := kindVar
					if d.Tok == token.CONST {
						kind = kindConst
					}
					for _, name := range s.Names {
						add(name, name.Name, kind, start, end)
					}
				}
			}
		}
	}
	return out
}

// receiverName is the type name of a method receiver expression (T, *T, T[K], *T[K, V]).
func receiverName(expr ast.Expr) string {
	for {
		switch e := expr.(type) {
		case *ast.StarExpr:
			expr = e.X
		case *ast.ParenExpr:
			expr = e.X
		case *ast.IndexExpr:
			expr = e.X
		case *ast.IndexListExpr:
			expr = e.X
		case *ast.Ident:
			return e.Name
		default:
			return ""
		}
	}
}

// enclosingName is the function a top-level declaration belongs to: "name", "Type.Method", or "" for variables,
// constants and types.
func enclosingName(decl ast.Decl) string {
	fn, ok := decl.(*ast.FuncDecl)
	if !ok {
		return ""
	}
	if fn.Recv != nil && len(fn.Recv.List) > 0 {
		if recv := receiverName(fn.Recv.List[0].Type); recv != "" {
			return recv + "." + fn.Name.Name
		}
	}
	return fn.Name.Name
}

// callIdents are the identifiers that are the function part of a call: f in f(), Sel in x.Sel(), and the same
// through an explicit instantiation (f[int]()).
func callIdents(file *ast.File) map[*ast.Ident]bool {
	calls := map[*ast.Ident]bool{}
	ast.Inspect(file, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		fun := ast.Unparen(call.Fun)
		for {
			switch f := fun.(type) {
			case *ast.Ident:
				calls[f] = true
			case *ast.SelectorExpr:
				calls[f.Sel] = true
			case *ast.IndexExpr:
				fun = ast.Unparen(f.X)
				continue
			case *ast.IndexListExpr:
				fun = ast.Unparen(f.X)
				continue
			}
			return true
		}
	})
	return calls
}

// externalName is how a function or method outside the module is named: "Name" or "Type.Method".
func externalName(fn *types.Func) (qualified, kind string) {
	sig, ok := fn.Type().(*types.Signature)
	if !ok || sig.Recv() == nil {
		return fn.Name(), kindFunction
	}
	t := sig.Recv().Type()
	if ptr, ok := t.(*types.Pointer); ok {
		t = ptr.Elem()
	}
	if named, ok := t.(*types.Named); ok {
		return named.Obj().Name() + "." + fn.Name(), kindMethod
	}
	return fn.Name(), kindMethod
}

func (ix *indexer) emitRefs() {
	type refKey struct {
		def       int
		path      string
		line      int
		enclosing string
		call      bool
	}
	seen := map[refKey]bool{}
	externalSeen := map[string]bool{}
	var externals []externalRef
	externalDefs := map[string]DefFact{}

	for _, cp := range ix.checked {
		for i, file := range cp.files {
			path, test, ok := ix.source(cp, cp.filenames[i])
			if !ok {
				continue
			}
			calls := callIdents(file)
			for _, decl := range file.Decls {
				enclosing := enclosingName(decl)
				ast.Inspect(decl, func(n ast.Node) bool {
					id, isIdent := n.(*ast.Ident)
					if !isIdent {
						return true
					}
					obj := cp.info.Uses[id]
					if obj == nil {
						return true
					}
					fn, isFunc := obj.(*types.Func)
					if isFunc {
						fn = fn.Origin()
						obj = fn
					}
					line := ix.fset.Position(id.Pos()).Line
					isCall := isFunc && calls[id]
					if obj.Pos().IsValid() {
						if def, found := ix.byKey[ix.posKey(obj.Pos())]; found {
							k := refKey{def, path, line, enclosing, isCall}
							if !seen[k] {
								seen[k] = true
								ix.facts.Refs = append(ix.facts.Refs, RefFact{
									Def: def, Path: path, Line: line, Enclosing: enclosing, Call: isCall, Test: test})
							}
							return true
						}
					}
					// A call to something outside the type-checked packages (standard library, dependencies) is kept
					// for callees: once per calling function and callee.
					if isCall && fn.Pkg() != nil && !ix.ownPaths[basePath(fn.Pkg().Path())] {
						qualified, kind := externalName(fn)
						key := fn.Pkg().Path() + "\x00" + qualified
						if _, known := externalDefs[key]; !known {
							externalDefs[key] = DefFact{Name: fn.Name(), Qualified: qualified, Kind: kind,
								Pkg: fn.Pkg().Path(), Exported: fn.Exported(), External: true}
						}
						once := fmt.Sprintf("%s|%s|%s|%t", key, path, enclosing, test)
						if !externalSeen[once] {
							externalSeen[once] = true
							externals = append(externals, externalRef{key: key, ref: RefFact{
								Path: path, Line: line, Enclosing: enclosing, Call: true, Test: test}})
						}
					}
					return true
				})
			}
		}
	}

	keys := make([]string, 0, len(externalDefs))
	for key := range externalDefs {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	ids := map[string]int{}
	for _, key := range keys {
		d := externalDefs[key]
		d.ID = len(ix.facts.Defs) + 1
		ids[key] = d.ID
		ix.facts.Defs = append(ix.facts.Defs, d)
	}
	for _, e := range externals {
		e.ref.Def = ids[e.key]
		ix.facts.Refs = append(ix.facts.Refs, e.ref)
	}
	sort.SliceStable(ix.facts.Refs, func(i, j int) bool {
		a, b := ix.facts.Refs[i], ix.facts.Refs[j]
		if a.Def != b.Def {
			return a.Def < b.Def
		}
		if a.Path != b.Path {
			return a.Path < b.Path
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		return a.Enclosing < b.Enclosing
	})
}

func (ix *indexer) emitPackages() {
	unresolvedSeen := map[string]bool{}
	for _, cp := range ix.checked {
		hasFile := false
		for i := range cp.files {
			if _, _, ok := ix.source(cp, cp.filenames[i]); ok {
				hasFile = true
				break
			}
		}
		if !hasFile {
			continue
		}
		dir, _ := ix.rel(cp.listed.Dir)
		imports := map[string]bool{}
		for _, imp := range cp.listed.Imports {
			if mapped, ok := cp.listed.ImportMap[imp]; ok {
				imp = mapped
			}
			if imp = basePath(imp); imp != "C" && imp != "unsafe" {
				imports[imp] = true
			}
		}
		pkg := PackageFact{Path: cp.base, Dir: dir, Module: cp.module, Test: cp.variant, Errors: []string{}}
		for imp := range imports {
			pkg.Imports = append(pkg.Imports, imp)
		}
		sort.Strings(pkg.Imports)
		if pkg.Imports == nil {
			pkg.Imports = []string{}
		}
		seenErr := map[string]bool{}
		for _, e := range cp.hardErrs {
			if e != "" && !seenErr[e] && len(pkg.Errors) < 20 {
				seenErr[e] = true
				pkg.Errors = append(pkg.Errors, e)
			}
		}
		if e := cp.listed.Error; e != nil {
			pos := e.Pos
			if rel, ok := ix.rel(pos); ok && ix.cfg.Readable[rel] {
				pkg.Errors = append(pkg.Errors, rel)
			} else if pos == "" {
				pkg.Errors = append(pkg.Errors, oneLine(e.Err, 200))
			}
		}
		ix.facts.Packages = append(ix.facts.Packages, pkg)
		if len(cp.hardErrs) > 0 {
			ix.unresolved(cp, unresolvedSeen)
		}
	}
	sort.SliceStable(ix.facts.Packages, func(i, j int) bool {
		a, b := ix.facts.Packages[i], ix.facts.Packages[j]
		if a.Path != b.Path {
			return a.Path < b.Path
		}
		return !a.Test && b.Test
	})
	sort.SliceStable(ix.facts.Unresolved, func(i, j int) bool {
		a, b := ix.facts.Unresolved[i], ix.facts.Unresolved[j]
		if a.Path != b.Path {
			return a.Path < b.Path
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		return a.Name < b.Name
	})
}

// unresolved records the identifiers of a package with type errors that denote nothing the checker could find.
func (ix *indexer) unresolved(cp *checkedPkg, seen map[string]bool) {
	for i, file := range cp.files {
		path, _, ok := ix.source(cp, cp.filenames[i])
		if !ok {
			continue
		}
		skip := map[*ast.Ident]bool{file.Name: true}
		ast.Inspect(file, func(n ast.Node) bool {
			switch s := n.(type) {
			case *ast.BranchStmt:
				if s.Label != nil {
					skip[s.Label] = true
				}
			case *ast.LabeledStmt:
				skip[s.Label] = true
			}
			return true
		})
		ast.Inspect(file, func(n ast.Node) bool {
			id, isIdent := n.(*ast.Ident)
			if !isIdent || skip[id] || id.Name == "_" || id.Name == "." {
				return true
			}
			if _, defined := cp.info.Defs[id]; defined {
				return true
			}
			if _, used := cp.info.Uses[id]; used {
				return true
			}
			if len(ix.facts.Unresolved) >= maxUnresolved {
				return false
			}
			line := ix.fset.Position(id.Pos()).Line
			key := fmt.Sprintf("%s:%d:%s", path, line, id.Name)
			if !seen[key] {
				seen[key] = true
				ix.facts.Unresolved = append(ix.facts.Unresolved, UnresolvedFact{
					Path: path, Line: line, Name: id.Name, Reason: "type error"})
			}
			return true
		})
	}
}

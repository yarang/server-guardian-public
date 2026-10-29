package main

// Version is the fact format version. cli/llm-agent/go_facts.py (GOINDEX_VERSION) must be bumped with it:
// the engine treats facts with another version as missing.
const Version = 1

// Facts is the JSON document goindex writes (docs/design/debate-code-index-wiki.md, "Fact format").
// It holds names, kinds and positions only: no source text, string literals or comments.
type Facts struct {
	Version      int              `json:"version"`
	SHA          string           `json:"sha"`
	Go           string           `json:"go"`
	GOOS         string           `json:"goos"`
	GOARCH       string           `json:"goarch"`
	Modules      []string         `json:"modules"`
	ModuleErrors []ModuleError    `json:"module_errors"`
	Packages     []PackageFact    `json:"packages"`
	Files        []string         `json:"files"`
	Defs         []DefFact        `json:"defs"`
	Refs         []RefFact        `json:"refs"`
	Unresolved   []UnresolvedFact `json:"unresolved"`
}

// ModuleError records a module that could not be indexed at all (the others still are).
type ModuleError struct {
	Dir   string `json:"dir"`
	Error string `json:"error"`
}

// PackageFact is one type-checked package. Test is true for the variant that holds _test.go files.
type PackageFact struct {
	Path    string   `json:"path"`
	Dir     string   `json:"dir"`
	Module  string   `json:"module"`
	Imports []string `json:"imports"`
	Test    bool     `json:"test"`
	Errors  []string `json:"errors"`
}

// DefFact is a package-level declaration, a method, or an interface method. External definitions (standard library
// and dependencies) exist only as targets of calls and have no path or lines.
type DefFact struct {
	ID        int    `json:"id"`
	Name      string `json:"name"`
	Qualified string `json:"qualified"`
	Kind      string `json:"kind"`
	Pkg       string `json:"pkg"`
	Path      string `json:"path,omitempty"`
	Start     int    `json:"start,omitempty"`
	End       int    `json:"end,omitempty"`
	Exported  bool   `json:"exported"`
	Test      bool   `json:"test"`
	External  bool   `json:"external,omitempty"`
}

// RefFact is a use of a definition. Enclosing is the function around it ("Type.Method" for methods, empty at
// package level). Call is true when the use is the function part of a call expression.
type RefFact struct {
	Def       int    `json:"def"`
	Path      string `json:"path"`
	Line      int    `json:"line"`
	Enclosing string `json:"enclosing"`
	Call      bool   `json:"call"`
	Test      bool   `json:"test"`
}

// UnresolvedFact is an identifier in a package with type errors that the checker could not resolve.
type UnresolvedFact struct {
	Path   string `json:"path"`
	Line   int    `json:"line"`
	Name   string `json:"name"`
	Reason string `json:"reason"`
}

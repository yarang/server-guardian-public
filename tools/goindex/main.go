// Command goindex extracts type-checked definitions and references from the Go modules of a checked-out tree.
//
//	goindex -root <worktree> -module <dir> [-module <dir>...] -readable <file> -out <json> [-sha <sha>] [-timeout 180s]
//
// -module directories are relative to -root and hold a go.mod. -readable names a file with the repository-relative
// paths (one per line) that may appear in the output; facts of any other file are dropped. The output holds names,
// kinds and positions only (see facts.go). The caller sets GOCACHE; CGO_ENABLED, GOTOOLCHAIN, GOFLAGS, GOPROXY and
// GOWORK are forced (see golist.go). Exit status: 0 facts written, 1 no module could be indexed (facts with
// module_errors are still written), 2 usage or I/O error, 3 timeout.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
)

type stringList []string

func (s *stringList) String() string     { return strings.Join(*s, ",") }
func (s *stringList) Set(v string) error { *s = append(*s, v); return nil }

func main() {
	os.Exit(run(os.Args[1:], os.Stderr))
}

func run(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("goindex", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", "", "worktree to index")
	readable := fs.String("readable", "", "file listing the repository-relative paths that may appear in the output")
	out := fs.String("out", "", "output JSON file")
	sha := fs.String("sha", "", "commit id to record in the output")
	timeout := fs.Duration("timeout", 0, "give up after this long (0: no limit)")
	var modules stringList
	fs.Var(&modules, "module", "module directory relative to -root (repeatable)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *root == "" || *out == "" || *readable == "" || len(modules) == 0 {
		fmt.Fprintln(stderr, "goindex: -root, -module, -readable and -out are required")
		return 2
	}
	allowed, err := readLines(*readable)
	if err != nil {
		fmt.Fprintf(stderr, "goindex: %v\n", err)
		return 2
	}
	ctx := context.Background()
	if *timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, *timeout)
		defer cancel()
	}
	facts, indexErr := Index(ctx, Config{Root: *root, Modules: modules, Readable: allowed, SHA: *sha})
	if errors.Is(indexErr, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
		fmt.Fprintln(stderr, "goindex: timeout")
		return 3
	}
	if facts == nil {
		fmt.Fprintf(stderr, "goindex: %v\n", indexErr)
		return 2
	}
	data, err := json.Marshal(facts)
	if err == nil {
		err = os.WriteFile(*out, data, 0o600)
	}
	if err != nil {
		fmt.Fprintf(stderr, "goindex: %v\n", err)
		return 2
	}
	if indexErr != nil {
		fmt.Fprintf(stderr, "goindex: %v\n", indexErr)
		return 1
	}
	return 0
}

func readLines(path string) (map[string]bool, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	set := map[string]bool{}
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		if line := strings.TrimSpace(sc.Text()); line != "" {
			set[line] = true
		}
	}
	return set, sc.Err()
}

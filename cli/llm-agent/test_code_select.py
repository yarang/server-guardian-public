import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from code_context import ContextError, build_pack, validate_patterns
from code_select import (
    go_imports,
    go_outline,
    go_symbol,
    merge_spans,
    parse_selector,
    python_imports,
    python_outline,
    python_symbol,
)

GO = '''package main

import (
\t"fmt"
)

// Server is a node.
type Server struct {
\tName string
}

type ID int

// Start runs it.
func (s *Server) Start() error {
\tif s == nil {
\t\treturn nil
\t}
\treturn nil
}

func helper(x int) int {
\treturn x
}

func main() {
\thandler := func() {
\t\tfmt.Println("inside main")
\t}
\thandler()
}
'''

PY = '''import os


@decorated
def top(a):
    return a


class Box:
    def open(self):
        return 1

    def close(self):
        return 2
'''


class SelectorParsingTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(parse_selector("a/b.go#Server.Start").symbol, "Server.Start")
        self.assertEqual(parse_selector("a/b.go:10-20").lines, (10, 20))
        self.assertIsNone(parse_selector("a/*.go").symbol)
        self.assertEqual(parse_selector("C:/x.go").glob, "C:/x.go")  # a colon without a range is part of the path
        for bad in ("a.go#", "a.go#1x", "a.go#a.b.c", "a.go:20-10", "a.go:0-3", "#Name", ":1-2"):
            with self.assertRaises(ValueError, msg=bad):
                parse_selector(bad)
        with self.assertRaises(ContextError):
            validate_patterns(["apps/x.go#bad name"])


class GoTests(unittest.TestCase):
    def test_symbols(self) -> None:
        self.assertEqual(go_symbol(GO, "Server"), [(7, 10)])  # leading comment included
        self.assertEqual(go_symbol(GO, "ID"), [(12, 12)])
        self.assertEqual(go_symbol(GO, "Server.Start"), [(14, 20)])
        self.assertEqual(go_symbol(GO, "helper"), [(22, 24)])
        self.assertEqual(go_symbol(GO, "Start"), [])  # methods need Type.Method
        self.assertEqual(go_symbol(GO, "missing"), [])

    def test_outline(self) -> None:
        outline = go_outline(GO)
        self.assertIn("1: package main", outline)
        self.assertFalse(any('"fmt"' in line for line in outline))  # imports are shown verbatim, not outlined
        self.assertEqual(go_imports(GO), [(3, 5)])
        self.assertEqual(go_imports('package x\n\nimport "os"\nimport f "fmt"\n'), [(3, 3), (4, 4)])
        self.assertIn("15: func (s *Server) Start() error", outline)
        self.assertFalse(any("inside main" in line for line in outline))


class PythonTests(unittest.TestCase):
    def test_symbols_and_outline(self) -> None:
        self.assertEqual(python_symbol(PY, "top"), [(4, 6)])  # decorator included
        self.assertEqual(python_symbol(PY, "Box.close"), [(13, 14)])
        self.assertEqual(python_symbol(PY, "Box"), [(9, 14)])
        self.assertEqual(python_symbol(PY, "close"), [])
        self.assertEqual(python_symbol("def (", "x"), [])
        self.assertIn("10:     def open(self):", python_outline(PY))
        self.assertFalse(any("import os" in line for line in python_outline(PY)))
        self.assertEqual(python_imports(PY), [(1, 1)])

    def test_merge(self) -> None:
        merged = merge_spans([((5, 8), "#a"), ((1, 3), "lines 1-3"), ((4, 6), "#b"), ((20, 21), "#c")])
        self.assertEqual(merged, [((1, 8), ["lines 1-3", "#b", "#a"]), ((20, 21), ["#c"])])


class PackSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "app").mkdir()
        (self.repo / "app/main.go").write_text(GO)
        (self.repo / "app/other.go").write_text("package main\n\nfunc other() {\n}\n")
        (self.repo / "tool.py").write_text(PY)
        (self.repo / "readme.md").write_text("line1\nline2\nline3\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "one"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_partial_files(self) -> None:
        pack = build_pack(self.repo, ["app/*.go#helper", "app/main.go:26-31", "tool.py#Box.open", "readme.md:2-2"])
        files = {f["path"]: f for f in pack.files}
        self.assertEqual(set(files), {"app/main.go", "tool.py", "readme.md"})  # other.go has no helper
        self.assertEqual(files["app/main.go"]["parts"], ["#helper", "lines 26-31", "imports"])
        self.assertIn('[imports: lines 3-5]\nimport (\n\t"fmt"\n)', pack.text)
        self.assertTrue(pack.saw("app/main.go", 'import (\n\t"fmt"\n)'))
        self.assertEqual(files["readme.md"]["parts"], ["lines 2-2"])  # no imports outside Go and Python
        self.assertTrue(files["app/main.go"]["outline"])
        self.assertFalse(files["readme.md"]["outline"])
        self.assertIn("inside main", pack.text)
        self.assertNotIn("return nil", pack.text)  # Start's body was not selected
        self.assertIn("[#Box.open: lines 10-11]", pack.text)
        self.assertIn("line2", pack.text)
        self.assertNotIn("line3", pack.text)
        self.assertIn("say which file and symbol", pack.text)

    def test_whole_file_wins_and_missing_symbol_reported(self) -> None:
        pack = build_pack(self.repo, ["app/main.go", "app/main.go#helper", "app/*.go#nothing"])
        self.assertNotIn("parts", pack.files[0])
        self.assertIn("return nil", pack.text)
        self.assertIn({"path": "app/*.go#nothing", "reason": "symbol not found"}, pack.snapshot["skipped"])
        self.assertNotIn("say which file and symbol", pack.text)

    def test_range_past_end_is_clamped(self) -> None:
        pack = build_pack(self.repo, ["readme.md:2-99"])
        self.assertIn("[lines 2-99: lines 2-3]", pack.text)


if __name__ == "__main__":
    unittest.main()

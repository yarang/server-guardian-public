import json
import os
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple

import code_index
from code_index import (
    INDEX_VERSION,
    CodeIndex,
    default_cache_dir,
    parse_file,
    parse_go,
    parse_python,
    parse_ts,
    scan_identifiers,
    ts_import_spans,
    ts_outline,
    ts_symbol_spans,
)
from code_select import outline, symbol_spans

TSX_SOURCE = '''import React, { useState } from "react";
import type { Props } from "./types";

// A comment mentioning Ghost that must not count.
export interface ButtonProps {
  label: string;
}

export type Size = "sm" | "lg";

type Internal<T> =
  | T
  | null;

export enum Mode {
  Fast,
  Slow,
}

export const DEFAULT_SIZE: Size = "sm";

const helper = (n: number): number => n + 1;

export const Counter = ({ label }: ButtonProps): JSX.Element => {
  const [count, setCount] = useState(0);
  const text = `count ${helper(count)} of Ghost`;
  return (
    <div className="Ghost">
      <Badge value={count} />
      <button onClick={() => setCount(helper(count))}>{label}</button>
    </div>
  );
};

export default function App(
  props: Props,
): JSX.Element {
  return <Counter label="x" />;
}

function plain() { return 1; }
export async function load(): Promise<void> {
  await fetch("/Ghost");
}

class Store {
  items: string[] = [];
}

export default class extends Store {
}
'''

GO_SOURCE = '''package main

import "fmt"

// Server serves things.
type Server struct {
	name string
}

type Handler interface {
	Handle()
}

const (
	Alpha = 1
	Beta  = 2
)

var global = "Ghost in a string"

// Handle handles a request.
func (s *Server) Handle(w int) {
	fmt.Println("Ghost", Alpha) // Ghost again
	helper()
}

func (g Stack[T]) Push(v T) {
}

func helper() {
	x := `raw
Ghost`
	_ = x
}

func one() int { return 1 }
'''

PY_SOURCE = '''"""Module docstring."""
import os
from pkg.mod import Thing

LIMIT = 10
name: str = "x"


@decorator
def top(a):
    return Thing(a).run() + LIMIT


async def go_async():
    pass


class Outer:
    """Doc."""

    def method(self):
        return self.other()

    class Inner:
        def deep(self):
            return 1
'''


def by_qualified(decls: List[Dict]) -> Dict[str, Dict]:
    return {d["qualified"]: d for d in decls}


class PythonParserTests(unittest.TestCase):
    def test_declarations_and_references(self) -> None:
        decls, refs = parse_python(PY_SOURCE)
        found = by_qualified(decls)
        self.assertEqual(set(found), {"LIMIT", "name", "top", "go_async", "Outer", "Outer.method", "Outer.Inner",
                                      "Outer.Inner.deep"})
        self.assertEqual(found["Outer.Inner.deep"]["kind"], "method")
        self.assertEqual(found["Outer.Inner.deep"]["name"], "deep")
        self.assertEqual(found["Outer.Inner.deep"]["signature"], "def deep(self):")
        self.assertEqual(found["LIMIT"]["kind"], "const")
        self.assertEqual(found["name"]["kind"], "variable")
        self.assertEqual((found["top"]["start"], found["top"]["end"]), (10, 11))
        self.assertEqual(found["Outer"]["end"], 26)
        self.assertEqual(refs["LIMIT"], [5, 11])
        self.assertEqual(refs["run"], [11])  # attribute call
        self.assertEqual(refs["Thing"], [3, 11])
        self.assertEqual(refs["other"], [22])
        self.assertIn(10, refs["top"])  # the def line itself

    def test_syntax_error_is_reported_by_parse_file(self) -> None:
        with self.assertRaises(SyntaxError):
            parse_python("def broken(:\n")
        self.assertEqual(parse_file("x.py", "def broken(:\n"), {"decls": [], "refs": {}, "error": True, "lines": 1})
        self.assertEqual(parse_file("x.py", "a\0b"), {"decls": [], "refs": {}, "error": True, "lines": 1})

    def test_signature_is_cut_to_200_characters(self) -> None:
        decls, _refs = parse_python("def f(" + ", ".join(f"arg{i}" for i in range(100)) + "):\n    pass\n")
        self.assertEqual(len(decls[0]["signature"]), 200)


class GoParserTests(unittest.TestCase):
    def test_declarations(self) -> None:
        decls, _refs = parse_go(GO_SOURCE)
        found = by_qualified(decls)
        self.assertEqual(set(found), {"Server", "Handler", "Alpha", "Beta", "global", "Server.Handle",
                                      "Stack.Push", "helper", "one"})
        method = found["Server.Handle"]
        self.assertEqual((method["name"], method["kind"], method["start"], method["end"]), ("Handle", "method", 22, 25))
        self.assertTrue(method["signature"].startswith("func (s *Server) Handle("))
        self.assertEqual(found["Server"]["kind"], "struct")
        self.assertEqual(found["Handler"]["kind"], "interface")
        self.assertEqual(found["Alpha"]["kind"], "const")
        self.assertEqual((found["Alpha"]["start"], found["Alpha"]["end"]), (15, 15))
        self.assertEqual(found["one"]["end"], found["one"]["start"])
        self.assertEqual(found["Stack.Push"]["name"], "Push")

    def test_references_skip_comments_strings_and_raw_strings(self) -> None:
        _decls, refs = parse_go(GO_SOURCE)
        self.assertNotIn("Ghost", refs)
        self.assertEqual(refs["helper"], [24, 30])
        self.assertEqual(refs["Alpha"], [15, 23])
        self.assertIn("Println", refs)
        self.assertNotIn("func", refs)  # keywords are dropped

    def test_unformatted_or_empty_input_does_not_raise(self) -> None:
        self.assertEqual(parse_go("")[0], [])
        decls, _ = parse_go("func broken( {\n\tx\n")
        self.assertEqual([d["name"] for d in decls], ["broken"])


class TypeScriptParserTests(unittest.TestCase):
    def test_tsx_component_file(self) -> None:
        decls, refs = parse_ts(TSX_SOURCE)
        found = by_qualified(decls)
        expected = {"ButtonProps", "Size", "Internal", "Mode", "DEFAULT_SIZE", "helper", "Counter", "App", "plain",
                    "load", "Store", "default"}
        self.assertEqual(set(found), expected)
        self.assertEqual([found[n]["kind"] for n in ("ButtonProps", "Size", "Mode", "DEFAULT_SIZE", "App", "Store")],
                         ["interface", "type", "enum", "const", "function", "class"])
        self.assertEqual((found["ButtonProps"]["start"], found["ButtonProps"]["end"]), (5, 7))
        self.assertEqual((found["Internal"]["start"], found["Internal"]["end"]), (11, 13))  # indented union lines
        self.assertEqual((found["Mode"]["start"], found["Mode"]["end"]), (15, 18))
        self.assertEqual(found["helper"]["end"], found["helper"]["start"])  # arrow function const on one line
        self.assertEqual((found["Counter"]["start"], found["Counter"]["end"]), (24, 33))
        # `): JSX.Element {` at column 0 continues the signature; the body closes at the next column-0 brace.
        self.assertEqual((found["App"]["start"], found["App"]["end"]), (35, 39))
        self.assertEqual(found["plain"]["end"], found["plain"]["start"])
        self.assertEqual(found["default"]["kind"], "class")
        self.assertEqual(found["default"]["end"], len(TSX_SOURCE.splitlines()))
        self.assertTrue(found["Counter"]["signature"].startswith("export const Counter ="))
        # References: JSX tags and expressions of template literals count, comments, strings and template text do not.
        for name in ("Badge", "Counter", "helper", "setCount", "useState", "Props"):
            self.assertIn(name, refs, name)
        self.assertEqual(refs["helper"], [22, 26, 30])
        self.assertEqual(refs["Counter"], [24, 38])
        self.assertNotIn("Ghost", refs)
        self.assertNotIn("return", refs)

    def test_plain_javascript_and_commonjs_style(self) -> None:
        source = ("const fs = require('fs');\nlet counter = 0, other = 1;\nvar legacy = function () {\n  return 1;\n};\n"
                  "export default async function main() {\n  counter++;\n}\nmodule.exports = { main };\n")
        decls, refs = parse_ts(source)
        self.assertEqual([(d["qualified"], d["start"], d["end"]) for d in decls],
                         [("fs", 1, 1), ("counter", 2, 2), ("legacy", 3, 5), ("main", 6, 8)])
        self.assertEqual(refs["counter"], [2, 7])
        self.assertEqual(refs["main"], [6, 9])

    def test_malformed_and_unusual_input_does_not_raise(self) -> None:
        for source in ("", "export function (", "type = 3\n", "export default class extends Foo {\n", "`unterminated ${",
                       "const x = `a ${b `c ${d}` e} f`;\n", "/* never closed\nfunction x() {}\n", "\x00\x01 junk"):
            decls, refs = parse_ts(source)
            self.assertIsInstance(decls, list)
            self.assertIsInstance(refs, dict)
        decls, _ = parse_ts("type = 3\ntype Foo = string;\n")
        self.assertEqual([d["name"] for d in decls], ["Foo"])  # `type = 3` is not a declaration

    def test_nested_template_expressions(self) -> None:
        refs = scan_identifiers("const x = `a ${b `c ${d}` e} f ${g}`;\nh();\n", ts=True)
        for name in ("b", "d", "g", "h", "x"):
            self.assertIn(name, refs)
        for text_word in ("a", "c", "f"):
            self.assertNotIn(text_word, refs)

    def test_outline_symbols_and_imports(self) -> None:
        lines = ts_outline(TSX_SOURCE)
        self.assertIn("5: export interface ButtonProps {", lines)
        self.assertIn("35: export default function App(", lines)
        self.assertEqual(ts_symbol_spans(TSX_SOURCE, "App"), [(35, 39)])
        self.assertEqual(ts_symbol_spans(TSX_SOURCE, "Nope"), [])
        self.assertEqual(ts_symbol_spans(TSX_SOURCE, "Store.items"), [])
        multi = "import {\n  a,\n  b,\n} from './x';\nimport './side';\nimport z from \"z\";\nconst q = 1;\n"
        self.assertEqual(ts_import_spans(multi), [(1, 4), (5, 5), (6, 6)])
        self.assertEqual(ts_import_spans("import {\n  a,\n"), [])  # unterminated: not an import statement

    def test_code_select_dispatches_typescript(self) -> None:
        self.assertEqual(symbol_spans("web/app.tsx", TSX_SOURCE, "Counter"), [(24, 33)])
        self.assertTrue(outline("web/app.mjs", "export const a = 1;\n"))
        self.assertEqual(outline("web/readme.md", "export const a = 1;\n"), [])


class ParseFileTests(unittest.TestCase):
    def test_language_dispatch(self) -> None:
        self.assertIsNone(parse_file("notes.md", "# hi"))
        for path in ("a.ts", "a.tsx", "a.js", "a.jsx", "a.mjs", "a.cjs"):
            self.assertEqual(parse_file(path, "const a = 1;\n")["decls"][0]["name"], "a", path)
        self.assertEqual(parse_file("a.go", "func f() {}\n")["decls"][0]["kind"], "function")


class FakeBlobs:
    """read_blobs callback over an in-memory {blob id: bytes}; counts what it was asked for."""

    def __init__(self, blobs: Dict[str, bytes]) -> None:
        self.blobs = blobs
        self.asked: List[str] = []

    def __call__(self, ids: List[str]) -> Dict[str, bytes]:
        self.asked.extend(ids)
        return {i: self.blobs[i] for i in ids if i in self.blobs}


def oid(n: int) -> str:
    return f"{n:040x}"


class CodeIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "cache"
        self.blobs = FakeBlobs({
            oid(1): b'package main\nfunc Handle() {\n\tprintln("SECRET_BODY")\n}\n',
            oid(2): b'export function Handle() {\n  log("SECRET_BODY");\n}\n',
            oid(3): b"def broken(:\n",
            oid(4): b"\x00\x01binary",
        })
        self.files = [("a.go", oid(1), 30), ("b.ts", oid(2), 30), ("c.py", oid(3), 12), ("d.go", oid(4), 8),
                      ("notes.md", oid(1), 5), ("huge.ts", oid(2), code_index.MAX_INDEXED_BYTES + 1)]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def index(self, cache: object = "default") -> CodeIndex:
        return CodeIndex(self.files, self.blobs, self.cache if cache == "default" else cache)  # type: ignore[arg-type]

    def test_build_without_cache(self) -> None:
        index = self.index(None).ensure()
        self.assertEqual([(d["path"], d["qualified"]) for d in index.decls], [("a.go", "Handle"), ("b.ts", "Handle")])
        self.assertEqual(index.errors, ["c.py"])
        self.assertEqual(index.skipped_large, ["huge.ts"])
        self.assertEqual(sorted(index.find_refs("Handle")), ["a.go", "b.ts"])
        self.assertEqual(index.find_defs("Nope"), [])
        self.assertFalse(self.cache.exists())

    def test_cache_files_hold_no_source_and_are_reused(self) -> None:
        first = self.index().ensure()
        self.assertEqual(first.stats["parsed"], 4)
        cached = sorted(self.cache.glob("blobs/*/*.json"))
        self.assertEqual(len(cached), 4)
        self.assertEqual(cached[0].parent.name, cached[0].name[:2])
        data = json.loads((self.cache / "blobs" / oid(1)[:2] / f"{oid(1)}.json").read_text())
        self.assertEqual(data["version"], INDEX_VERSION)
        self.assertEqual(data["decls"][0]["qualified"], "Handle")
        for path in cached:
            self.assertNotIn("SECRET_BODY", path.read_text())  # signatures are kept, bodies are not
            self.assertNotIn("package main", path.read_text())
        self.assertEqual(list(self.cache.rglob(".tmp-*")), [])
        self.blobs.asked.clear()
        second = self.index().ensure()
        self.assertEqual(self.blobs.asked, [])
        self.assertEqual((second.stats["parsed"], second.stats["cache_hits"]), (0, 4))
        self.assertEqual(second.decls, first.decls)
        self.assertEqual(second.errors, ["c.py"])

    def test_version_mismatch_and_corrupt_files_are_reparsed(self) -> None:
        self.index().ensure()
        stale = self.cache / "blobs" / oid(1)[:2] / f"{oid(1)}.json"
        data = json.loads(stale.read_text())
        data["version"] = INDEX_VERSION + 1
        stale.write_text(json.dumps(data))
        (self.cache / "blobs" / oid(2)[:2] / f"{oid(2)}.json").write_text("{not json")
        (self.cache / "blobs" / oid(3)[:2] / f"{oid(3)}.json").write_text(json.dumps({"version": INDEX_VERSION, "lang": "python"}))
        self.blobs.asked.clear()
        again = self.index().ensure()
        self.assertEqual(sorted(self.blobs.asked), sorted([oid(1), oid(2), oid(3)]))
        self.assertEqual(again.stats["cache_hits"], 1)
        self.assertEqual(json.loads(stale.read_text())["version"], INDEX_VERSION)  # rewritten

    def test_line_counts_are_indexed_and_cache_files_without_them_are_reparsed(self) -> None:
        first = self.index().ensure()
        self.assertTrue(first.lines and all(isinstance(n, int) and n >= 0 for n in first.lines.values()))
        old = self.cache / "blobs" / oid(1)[:2] / f"{oid(1)}.json"
        data = json.loads(old.read_text())
        del data["lines"]  # a P1 cache file
        old.write_text(json.dumps(data))
        self.blobs.asked.clear()
        again = self.index().ensure()
        self.assertEqual(self.blobs.asked, [oid(1)])
        self.assertEqual(again.lines, first.lines)

    def test_unwritable_cache_dir_falls_back_to_memory(self) -> None:
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("a file where a directory is needed")
        index = self.index(blocker / "sub").ensure()
        self.assertEqual(len(index.decls), 2)
        self.assertEqual(index.find_refs("Handle").keys(), {"a.go", "b.ts"})
        if os.name == "posix" and os.geteuid() != 0:
            readonly = Path(self._tmp.name) / "readonly"
            readonly.mkdir()
            readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                self.assertEqual(len(self.index(readonly).ensure().decls), 2)
            finally:
                readonly.chmod(stat.S_IRWXU)

    def test_a_blob_id_that_is_not_hex_never_touches_the_filesystem(self) -> None:
        index = CodeIndex([("a.go", "../../escape", 5)], FakeBlobs({"../../escape": b"func A() {}\n"}), self.cache)
        self.assertEqual([d["name"] for d in index.ensure().decls], ["A"])
        self.assertFalse(self.cache.exists())

    def test_same_blob_in_two_languages_is_parsed_per_language(self) -> None:
        shared = FakeBlobs({oid(9): b"func Same() {}\n"})
        index = CodeIndex([("x.go", oid(9), 14), ("y.ts", oid(9), 14)], shared, self.cache).ensure()
        self.assertEqual(sorted((d["path"], d["name"]) for d in index.decls), [("x.go", "Same")])
        self.assertEqual(shared.asked, [oid(9)])  # read once

    def test_default_cache_dir_follows_guardian_data_dir_and_creates_nothing(self) -> None:
        old = os.environ.get("GUARDIAN_DATA_DIR")
        os.environ["GUARDIAN_DATA_DIR"] = str(Path(self._tmp.name) / "data")
        try:
            target = default_cache_dir("my project/../x")
            self.assertEqual(target, Path(self._tmp.name) / "data" / "code-index" / "my_project_.._x")
            self.assertFalse((Path(self._tmp.name) / "data").exists())
            self.assertIsNone(default_cache_dir(None))
            self.assertIsNone(default_cache_dir(".."))
        finally:
            if old is None:
                del os.environ["GUARDIAN_DATA_DIR"]
            else:
                os.environ["GUARDIAN_DATA_DIR"] = old

    def test_concurrent_ensure_builds_once(self) -> None:
        import threading
        index = self.index(None)
        threads = [threading.Thread(target=index.ensure) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(index.stats["parsed"], 4)
        self.assertEqual(len(index.decls), 2)


class SignatureRedactionTests(unittest.TestCase):
    def test_literals_are_redacted_in_every_language(self) -> None:
        cases = {
            "python": ("x.py", 'API_KEY = "abc\\"def"  # note\nTOKEN: str = \'t0k\'\nRAW = rb"raw"\nPLAIN = 3\n',
                       ['API_KEY = "…"  # note', 'TOKEN: str = "…"', 'RAW = "…"', "PLAIN = 3"]),
            "go": ("x.go", 'package x\nconst Token = "abc"\nvar Raw = `raw ` + "z"\nfunc F(a string) rune { return \'q\' }\n',
                   ['const Token = "…"', 'var Raw = "…" + "…"', 'func F(a string) rune { return "…" }']),
            "ts": ("x.ts", "export const KEY = 'sec' + `tpl ${x}`;\nexport const B = \"two\";\nexport function f(a = 'd') {}\n",
                   ['export const KEY = "…" + "…";', 'export const B = "…";', 'export function f(a = "…") {}']),
        }
        for lang, (path, source, expected) in cases.items():
            parsed = parse_file(path, source)
            assert parsed is not None
            self.assertEqual([d["signature"] for d in parsed["decls"]], expected, lang)

    def test_unterminated_literals_are_redacted_to_the_end_of_the_line(self) -> None:
        self.assertEqual(code_index.redact_literals('x = """multi line secret'), 'x = "…"')
        self.assertEqual(code_index.redact_literals('x = "dangling \\'), 'x = "…"')
        self.assertEqual(code_index.redact_literals("no literals here"), "no literals here")
        self.assertEqual(code_index.redact_literals('ab"x" + b\'y\''), 'ab"…" + "…"')

    def test_redaction_happens_before_the_cut_to_200_characters(self) -> None:
        decls, _refs = parse_python('SECRET = "' + "s" * 500 + '"\n')
        self.assertEqual(decls[0]["signature"], 'SECRET = "…"')

    def test_cache_files_hold_no_literal_values(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            blobs = FakeBlobs({oid(1): b'API_KEY = "hunter2-value"\n\ndef f(x="default-secret"):\n    return 1\n',
                               oid(2): b"package p\nconst Pw = `raw-secret`\n",
                               oid(3): b"export const T = 'ts-secret';\n"})
            index = CodeIndex([("a.py", oid(1), 50), ("b.go", oid(2), 30), ("c.ts", oid(3), 30)], blobs, cache).ensure()
            for path in cache.rglob("*.json"):
                text = path.read_text()
                for leaked in ("hunter2-value", "default-secret", "raw-secret", "ts-secret"):
                    self.assertNotIn(leaked, text)
            self.assertIn('API_KEY = "…"', [d["signature"] for d in index.decls])

    def test_version_two_invalidates_version_one_cache_files_that_kept_literals(self) -> None:
        self.assertGreaterEqual(INDEX_VERSION, 2)
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            blobs = FakeBlobs({oid(1): b'API_KEY = "abc"\n'})
            CodeIndex([("a.py", oid(1), 20)], blobs, cache).ensure()
            target = cache / "blobs" / oid(1)[:2] / f"{oid(1)}.json"
            old = json.loads(target.read_text())
            old["version"] = 1
            old["decls"][0]["signature"] = 'API_KEY = "abc"'
            target.write_text(json.dumps(old))
            blobs.asked.clear()
            again = CodeIndex([("a.py", oid(1), 20)], blobs, cache).ensure()
            self.assertEqual(blobs.asked, [oid(1)])
            self.assertEqual(again.decls[0]["signature"], 'API_KEY = "…"')

    def test_outlines_used_for_context_packs_keep_the_real_code(self) -> None:
        ts = "export const KEY = 'sec';\nexport function f() {}\n"
        self.assertEqual(ts_outline(ts)[0], "1: export const KEY = 'sec';")
        self.assertEqual(outline("a.ts", ts)[0], "1: export const KEY = 'sec';")
        self.assertEqual(outline("a.go", 'package x\nconst Token = "abc"\n'), ["1: package x", '2: const Token = "abc"'])
        self.assertEqual(outline("a.py", 'def f(x="v"):\n    pass\n'), ['1: def f(x="v"):'])


class CachePruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "cache"
        self.now = 2_000_000_000.0
        code_index._PRUNED.clear()
        self._env = {k: os.environ.pop(k, None)
                     for k in ("GUARDIAN_CODE_INDEX_MAX_AGE_DAYS", "GUARDIAN_CODE_INDEX_MAX_MB")}

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        code_index._PRUNED.clear()
        self._tmp.cleanup()

    def blob(self, n: int, age_days: float, size: int = 100, version: Any = None) -> Path:
        name = f"{n:02x}" + "0" * 38  # a different shard directory per blob
        path = self.cache / "blobs" / name[:2] / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        head = json.dumps({"version": code_index.INDEX_VERSION if version is None else version, "pad": ""})[:-2]
        path.write_text(head + "x" * max(size - len(head) - 2, 0) + '"}')
        stamp = self.now - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_files_older_than_the_age_limit_are_deleted(self) -> None:
        old, fresh = self.blob(1, 31), self.blob(2, 29)
        self.assertEqual(code_index.prune_cache(self.cache, now=self.now), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse(old.parent.exists())  # the empty shard directory goes too

    def test_files_of_another_index_version_are_deleted_at_once(self) -> None:
        old_version, unreadable, current = self.blob(1, 0, version=1), self.blob(2, 0), self.blob(3, 0)
        unreadable.write_text("{not json")
        os.utime(unreadable, (self.now, self.now))
        self.assertEqual(code_index.prune_cache(self.cache, now=self.now), 2)
        self.assertEqual([p.exists() for p in (old_version, unreadable, current)], [False, False, True])

    def test_age_limit_is_configurable_and_zero_disables_it(self) -> None:
        old = self.blob(1, 31)
        os.environ["GUARDIAN_CODE_INDEX_MAX_AGE_DAYS"] = "0"
        self.assertEqual(code_index.prune_cache(self.cache, now=self.now), 0)
        self.assertTrue(old.exists())
        os.environ["GUARDIAN_CODE_INDEX_MAX_AGE_DAYS"] = "40"
        self.assertEqual(code_index.prune_cache(self.cache, now=self.now), 0)
        os.environ["GUARDIAN_CODE_INDEX_MAX_AGE_DAYS"] = "10"
        self.assertEqual(code_index.prune_cache(self.cache, now=self.now), 1)

    def test_over_the_size_limit_deletes_the_oldest_first(self) -> None:
        mb = 1024 * 1024
        files = [self.blob(1, 5, mb), self.blob(2, 3, mb), self.blob(3, 1, mb), self.blob(4, 4, mb)]
        os.environ["GUARDIAN_CODE_INDEX_MAX_MB"] = "2"
        self.assertEqual(code_index.prune_cache(self.cache, now=self.now), 2)
        self.assertEqual([p.exists() for p in files], [False, True, True, False])  # the 5 and 4 day old files went

    def test_other_files_are_never_touched(self) -> None:
        keep = self.cache / "notes.txt"
        self.cache.mkdir(parents=True)
        keep.write_text("mine")
        os.utime(keep, (1, 1))
        stale_tmp = self.blob(1, 0).with_name(".tmp-abc.json")
        stale_tmp.write_text("{")
        os.utime(stale_tmp, (self.now - 7200, self.now - 7200))
        code_index.prune_cache(self.cache, now=self.now)
        self.assertTrue(keep.exists())
        self.assertFalse(stale_tmp.exists())  # an interrupted write older than an hour
        self.assertEqual(code_index.prune_cache(self.cache / "missing", now=self.now), 0)

    def test_cache_hit_refreshes_the_mtime(self) -> None:
        blobs = FakeBlobs({oid(1): b"func A() {}\n"})
        CodeIndex([("a.go", oid(1), 12)], blobs, self.cache).ensure()
        target = self.cache / "blobs" / oid(1)[:2] / f"{oid(1)}.json"
        os.utime(target, (1_000_000, 1_000_000))
        CodeIndex([("a.go", oid(1), 12)], blobs, self.cache).ensure()
        self.assertGreater(target.stat().st_mtime, 1_500_000_000)

    def test_first_cache_write_prunes_once_per_process(self) -> None:
        old = self.blob(7, 40)
        stale = time.time() - 40 * 86400  # prune_cache runs against the real clock here
        os.utime(old, (stale, stale))
        blobs = FakeBlobs({oid(1): b"func A() {}\n", oid(2): b"func B() {}\n"})
        CodeIndex([("a.go", oid(1), 12)], blobs, self.cache).ensure()
        self.assertFalse(old.exists())
        again = self.blob(8, 0)
        os.utime(again, (stale, stale))
        CodeIndex([("b.go", oid(2), 12)], blobs, self.cache).ensure()
        self.assertTrue(again.exists())  # the second write in the same process does not prune again


if __name__ == "__main__":
    unittest.main()

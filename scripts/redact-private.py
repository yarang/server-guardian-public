#!/usr/bin/env python3
"""
Masks private names (hosts, sites, accounts, key files) in the repository's text files with public placeholders.

The rules are NOT in the repository: they map placeholders back to real hosts and people, so they live in
~/.config/server-guardian/public-redactions.txt (override with --rules). The file uses git-filter-repo's
--replace-text format, so scripts/export-public.sh rewrites the history with the very same rules:

    literal text==>replacement
    regex:pattern==>replacement          (Python re, applied top to bottom; # starts a comment)

Usage:
    scripts/redact-private.py [--rules FILE] [--check] [path ...]

    --check   change nothing; list every remaining match and exit 1 when there is one
    path ...  files to process (default: every file git tracks, binary files skipped)
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

DEFAULT_RULES = Path("~/.config/server-guardian/public-redactions.txt").expanduser()
Rule = Tuple[re.Pattern, str]


def load_rules(path: Path) -> List[Rule]:
    rules: List[Rule] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        left, sep, right = line.partition("==>")
        if not sep:
            sys.exit(f"{path}:{number}: a rule needs '==>'")
        if left.startswith("regex:"):
            rules.append((re.compile(left[len("regex:"):]), right))
        else:
            rules.append((re.compile(re.escape(left)), right.replace("\\", "\\\\")))
    return rules


def tracked_files(root: Path) -> List[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [root / name for name in out.decode("utf-8").split("\0") if name]


def text_of(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if not args.rules.is_file():
        print(f"redaction rules not found: {args.rules}", file=sys.stderr)
        return 2
    rules = load_rules(args.rules)
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                               check=True).stdout.strip())
    files = args.paths or tracked_files(root)
    found = changed = 0
    for path in files:
        text = text_of(path)
        if text is None:
            continue
        if args.check:
            for number, line in enumerate(text.splitlines(), 1):
                for pattern, _ in rules:
                    match = pattern.search(line)
                    if match:
                        found += 1
                        print(f"{path.relative_to(root) if path.is_relative_to(root) else path}:{number}: {match.group(0)}")
                        break
            continue
        new = text
        for pattern, replacement in rules:
            new = pattern.sub(replacement, new)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
    if args.check:
        print(f"{found} remaining match(es)", file=sys.stderr)
        return 1 if found else 0
    print(f"{changed} file(s) changed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

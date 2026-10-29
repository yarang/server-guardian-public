"""Seeded-defect corpus for the deterministic artifact validators (design-project.md, P2 evaluation).

10 healthy artifacts and 30 planted defects (five classes of six). `measure()` runs `artifact_ops.validate` over
both and reports recall per class and precision. Only findings that a defect adds to its healthy base count, so a
warning the healthy file already has never makes a defect look detected. The classes whose detector does not exist
yet (low contrast, false claims about code) stay in the corpus and score zero: the number is the gap, not a failure
of the harness. False claims are measured through the debate, not here (design-project.md).

    python3 artifact_corpus.py [--json] [--targets] [--min-recall CLASS=0.8 ...]

Standard library only. Nothing is read from or written to the data directory.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import artifact_ops

TYPES = ["markdown", "mermaid", "svg"]
SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'

HEALTHY: Dict[str, str] = {
    "overview.md": """# Service overview

The hub serves a dashboard and a read-only artifact API.

## Components

- The agent registers the artifact routes in `artifact_handler.go`.
- The topology is drawn in [the topology figure](topology.svg).
- The request flow is in [flow.mmd](flow.mmd).

## Limits

Each file is capped at 2 MiB.
""",
    "decisions.md": """# Decision record

## Context

Artifacts are stored as immutable versions.

## Decision

Keep the latest version forever and expire older ones after 30 days.

## Consequences

An expired version stays listed as a tombstone. See [the overview](overview.md).
""",
    "api-notes.md": """# API notes

## Routes

The agent registers five GET routes for artifacts.

## Headers

Every response carries `X-Content-Type-Options: nosniff` and a sandbox policy.

![Legend of the status colours](legend.svg)
""",
    "runbook.md": """# Runbook

## Check

Run the prune with `--dry-run` first and read the list.

## Rollback

Use the previous release directory; see [the decisions](decisions.md).
""",
    "logo.svg": f'<svg {SVG_NS} viewBox="0 0 40 40"><title>Guardian mark</title>'
                '<rect id="shield" x="4" y="4" width="32" height="32" fill="#1a3a6b"/>'
                '<text x="8" y="26" fill="#ffffff" font-size="14">SG</text></svg>',
    "topology.svg": f'<svg {SVG_NS} viewBox="0 0 120 60"><title>Topology</title>'
                    '<defs><rect id="node" width="30" height="16" fill="#0b3d2e"/></defs>'
                    '<use href="#node" x="5" y="5"/><use href="#node" x="80" y="5"/>'
                    '<text x="8" y="40" fill="#111111" font-size="8">hub</text></svg>',
    "legend.svg": f'<svg {SVG_NS} viewBox="0 0 80 30"><title>Status colours</title>'
                  '<rect id="ok" width="12" height="12" fill="#1b5e20"/>'
                  '<text x="16" y="10" fill="#000000" font-size="9">ok</text></svg>',
    "flow.mmd": "flowchart LR\n  A[Browser] --> B(Hub)\n  B --> C[(Artifact store)]\n",
    "sequence.mmd": "sequenceDiagram\n  participant U as User\n  participant H as Hub\n  U->>H: GET manifest\n  H-->>U: 200\n",
    "state.mmd": "stateDiagram-v2\n  [*] --> Latest\n  Latest --> Expired: 30 days\n  Expired --> [*]\n",
}

# The project setting artifacts.required_sections the corpus runs with: file -> headings it must contain.
REQUIRED_SECTIONS: Dict[str, List[str]] = {
    "decisions.md": ["Context", "Decision", "Consequences"],
    "runbook.md": ["Check", "Rollback"],
    "overview.md": ["Components", "Limits"],
    "api-notes.md": ["Routes", "Headers"],
}

# class -> pattern of a finding that counts as the right detection. None: no detector exists yet.
EXPECT: Dict[str, Optional[str]] = {
    "broken-svg": r"not well-formed|root element|is used \d+ times|reference to #|not an allowed|external reference|outside the SVG",
    "dead-link": r"(?:link to|image) '[^']*', which is not an artifact",
    "low-contrast": None,
    "missing-section": r"the required section '[^']*' is missing",
    "false-claim": None,
}

# The P2 recall targets (design-project.md): classes that have a deterministic detector. Low contrast waits for its
# detector; false claims are measured through the debate, so neither has a target here.
TARGETS: Dict[str, float] = {"broken-svg": 1.0, "dead-link": 1.0, "missing-section": 1.0}


@dataclass(frozen=True)
class Defect:
    ident: str
    cls: str
    base: str
    old: str
    new: str


DEFECTS: List[Defect] = [
    # broken SVG
    Defect("svg-unclosed", "broken-svg", "logo.svg", "</text></svg>", "</text>"),
    Defect("svg-no-namespace", "broken-svg", "logo.svg", f"<svg {SVG_NS} ", "<svg "),
    Defect("svg-duplicate-id", "broken-svg", "legend.svg", '<text x="16"', '<text id="ok" x="16"'),
    Defect("svg-dangling-ref", "broken-svg", "topology.svg", 'href="#node" x="80"', 'href="#nodes" x="80"'),
    Defect("svg-script", "broken-svg", "topology.svg", "</defs>", "<script>alert(1)</script></defs>"),
    Defect("svg-external-href", "broken-svg", "legend.svg", "</svg>", '<image href="https://example.invalid/a.png"/></svg>'),
    # low contrast (colour pair text on background)
    Defect("contrast-logo-text", "low-contrast", "logo.svg", 'fill="#ffffff" font-size="14"', 'fill="#2a4a7b" font-size="14"'),
    Defect("contrast-logo-bg", "low-contrast", "logo.svg", 'fill="#1a3a6b"', 'fill="#e8e8e8"'),
    Defect("contrast-topology-text", "low-contrast", "topology.svg", 'fill="#111111"', 'fill="#0f4030"'),
    Defect("contrast-topology-node", "low-contrast", "topology.svg", 'fill="#0b3d2e"', 'fill="#f5f5f5"'),
    Defect("contrast-legend-text", "low-contrast", "legend.svg", 'fill="#000000"', 'fill="#2b6f30"'),
    Defect("contrast-legend-swatch", "low-contrast", "legend.svg", 'fill="#1b5e20"', 'fill="#ffffff"'),
    # dead links (four hyperlinks, two image targets)
    Defect("link-overview-figure", "dead-link", "overview.md", "(topology.svg)", "(topologies.svg)"),
    Defect("link-overview-flow", "dead-link", "overview.md", "(flow.mmd)", "(flows.mmd)"),
    Defect("link-decisions-overview", "dead-link", "decisions.md", "(overview.md)", "(summary.md)"),
    Defect("link-runbook-decisions", "dead-link", "runbook.md", "(decisions.md)", "(decision-log.md)"),
    Defect("image-api-legend", "dead-link", "api-notes.md", "(legend.svg)", "(legends.svg)"),
    Defect("image-api-legend-dir", "dead-link", "api-notes.md", "(legend.svg)", "(figures/legend.svg)"),
    # missing required sections
    Defect("section-decision", "missing-section", "decisions.md", "## Decision\n\nKeep the latest version forever and expire older ones after 30 days.\n\n", ""),
    Defect("section-consequences", "missing-section", "decisions.md", "## Consequences\n\nAn expired version stays listed as a tombstone. See [the overview](overview.md).\n", ""),
    Defect("section-context", "missing-section", "decisions.md", "## Context\n\nArtifacts are stored as immutable versions.\n\n", ""),
    Defect("section-rollback", "missing-section", "runbook.md", "## Rollback\n\nUse the previous release directory; see [the decisions](decisions.md).\n", ""),
    Defect("section-limits", "missing-section", "overview.md", "\n## Limits\n\nEach file is capped at 2 MiB.\n", ""),
    Defect("section-headers", "missing-section", "api-notes.md", "## Headers\n\nEvery response carries `X-Content-Type-Options: nosniff` and a sandbox policy.\n\n", ""),
    # false claims about code
    Defect("claim-route-count", "false-claim", "api-notes.md", "five GET routes", "seven GET routes"),
    Defect("claim-file", "false-claim", "overview.md", "`artifact_handler.go`", "`artifact_server.go`"),
    Defect("claim-cap", "false-claim", "overview.md", "capped at 2 MiB", "capped at 64 MiB"),
    Defect("claim-retention", "false-claim", "decisions.md", "after 30 days", "after 90 days"),
    Defect("claim-header", "false-claim", "api-notes.md", "`X-Content-Type-Options: nosniff`", "`X-Frame-Options: deny`"),
    Defect("claim-dry-run", "false-claim", "runbook.md", "`--dry-run`", "`--force`"),
]


def apply(defect: Defect) -> str:
    base = HEALTHY[defect.base]
    if base.count(defect.old) != 1:
        raise ValueError(f"{defect.ident}: {defect.old!r} must occur exactly once in {defect.base}")
    return base.replace(defect.old, defect.new)


def findings(name: str, content: str) -> List[str]:
    report = artifact_ops.validate(name, content, TYPES, known=sorted(HEALTHY), required_sections=REQUIRED_SECTIONS.get(name))
    return report.errors + report.warnings


def measure() -> Dict[str, Any]:
    baseline = {name: findings(name, text) for name, text in HEALTHY.items()}
    flagged_healthy = sorted(name for name, found in baseline.items() if found)
    classes: Dict[str, Dict[str, Any]] = {}
    for defect in DEFECTS:
        added = [f for f in findings(defect.base, apply(defect)) if f not in baseline[defect.base]]
        pattern = EXPECT[defect.cls]
        right = [f for f in added if pattern and re.search(pattern, f)]
        row = classes.setdefault(defect.cls, {"detector": pattern is not None, "defects": 0, "flagged": 0, "detected": 0,
                                              "findings": 0, "right_findings": 0, "missed": [], "misflagged": []})
        row["defects"] += 1
        row["flagged"] += bool(added)
        row["detected"] += bool(right)
        row["findings"] += len(added)
        row["right_findings"] += len(right)
        if not right:
            row["missed"].append(defect.ident)
        if added and not right:
            row["misflagged"].append(defect.ident)
    detected = sum(r["detected"] for r in classes.values())
    flagged = sum(r["flagged"] for r in classes.values())
    return {
        "healthy": len(HEALTHY),
        "healthy_flagged": flagged_healthy,
        "defects": len(DEFECTS),
        "recall": detected / len(DEFECTS),
        # a flag is right when it names the planted defect's class; healthy artifacts that are flagged are wrong flags
        "precision": detected / (flagged + len(flagged_healthy)) if flagged + len(flagged_healthy) else None,
        "classes": {cls: dict(row, recall=row["detected"] / row["defects"]) for cls, row in classes.items()},
    }


def render(result: Dict[str, Any]) -> str:
    lines = [f"healthy artifacts: {result['healthy']} (flagged: {', '.join(result['healthy_flagged']) or 'none'})",
             f"defects: {result['defects']}  recall {result['recall']:.2f}  precision "
             f"{'n/a' if result['precision'] is None else format(result['precision'], '.2f')}", ""]
    lines.append(f"{'class':<16}{'detector':<10}{'recall':>8}{'detected':>10}{'flagged':>9}  missed")
    for cls, row in result["classes"].items():
        lines.append(f"{cls:<16}{'yes' if row['detector'] else 'none':<10}{row['recall']:>8.2f}"
                     f"{row['detected']:>6}/{row['defects']:<3}{row['flagged']:>9}  {', '.join(row['missed']) or '-'}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--targets", action="store_true", help="hold every class in TARGETS to its P2 target")
    parser.add_argument("--min-recall", action="append", default=[], metavar="CLASS=VALUE",
                        help="exit 1 when a class stays below the value (repeatable); without --targets or this, no target applies")
    args = parser.parse_args(argv)
    result = measure()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    failed = []
    for item in [*(f"{cls}={value}" for cls, value in TARGETS.items() if args.targets), *args.min_recall]:
        cls, _, value = item.partition("=")
        if cls not in result["classes"] or not value:
            parser.error(f"--min-recall expects CLASS=VALUE with a known class, got {item!r}")
        if result["classes"][cls]["recall"] < float(value):
            failed.append(f"{cls} {result['classes'][cls]['recall']:.2f} < {value}")
    if failed:
        print("below target: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

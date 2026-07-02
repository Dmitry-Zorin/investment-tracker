#!/usr/bin/env python3
"""Render generated chart SVGs to PNG for visual review.

The charts are SVGs and their correctness is visual, so this gives a repeatable
way to *look* at them without hand-running the pipeline and opening files. It
builds the report + market-analysis packages from a workspace, then rasterizes
every chart to PNG with macOS `qlmanage` (WebKit — the renderer that matches how
they actually display).

Usage (from the repo root):

  # build from the bundled fixture, then rasterize every chart
  python3 tools/render_charts.py

  # build from a specific workspace (e.g. your real one)
  python3 tools/render_charts.py --workspace /path/to/workspace

  # skip the build and just rasterize an existing charts/reports directory
  python3 tools/render_charts.py --charts reports/

PNGs land in --out (default: .chart-preview/ in the repo, which is gitignored).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO / "fixtures" / "mock-workspace"


def _rasterize(svgs: list[Path], out_dir: Path, size: int) -> list[Path]:
    if shutil.which("qlmanage") is None:
        sys.exit("qlmanage not found; this renderer requires macOS.")
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["qlmanage", "-t", "-s", str(size), "-o", str(out_dir), *map(str, svgs)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return [out_dir / f"{svg.name}.png" for svg in svgs]


def _build(workspace: Path) -> Path:
    for command in ("build", "export-market-analysis"):
        subprocess.run(
            [sys.executable, "-m", "investment_tracker", "--workspace", str(workspace), command],
            cwd=REPO,
            check=True,
        )
    return workspace / "reports"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, help="workspace to build from (default: bundled fixture, copied to a temp dir)")
    parser.add_argument("--charts", type=Path, help="rasterize SVGs under this directory instead of building")
    parser.add_argument("--out", type=Path, default=REPO / ".chart-preview", help="output directory for PNGs")
    parser.add_argument("--size", type=int, default=1400, help="qlmanage thumbnail size (longest side)")
    args = parser.parse_args()

    temp_dir: str | None = None
    if args.charts is not None:
        source = args.charts
    elif args.workspace is not None:
        source = _build(args.workspace)
    else:
        temp_dir = tempfile.mkdtemp(prefix="chart-preview-")
        workspace = Path(temp_dir) / "workspace"
        shutil.copytree(DEFAULT_FIXTURE, workspace)
        print(f"Using bundled fixture (single instrument) copied to {workspace}")
        source = _build(workspace)

    svgs = sorted(source.rglob("*.svg"))
    if not svgs:
        sys.exit(f"no .svg files under {source}")
    pngs = _rasterize(svgs, args.out, args.size)
    print(f"\nRendered {len(pngs)} chart(s) to {args.out}:")
    for png in pngs:
        print(f"  {png}")
    if temp_dir is not None:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

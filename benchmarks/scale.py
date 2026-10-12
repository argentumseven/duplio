"""Time and memory of Duplio's main commands on a large synthetic tree.

    python benchmarks/scale.py --files 300000 --dir /some/disk/with/space

It writes ``--files`` files (most of them small, a few large, ``--dup-fraction`` of them copies of another file)
into ``--dir/tree``, makes a second tree of hard links with 1 % of the files changed, and then runs the real command line
for each task in a clean child process, reporting wall time and the child's peak resident memory.

The numbers depend entirely on the machine and the file system, so the report prints both. Nothing here measures a
network share: round trips there dominate everything (see docs/DESIGN-NOTES.md).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

WRAPPER = """
import resource, subprocess, sys, time, json
start = time.perf_counter()
proc = subprocess.run(sys.argv[1:], capture_output=True, text=True)
elapsed = time.perf_counter() - start
rss_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(json.dumps({"seconds": elapsed, "rss_mib": rss_kib / 1024, "exit": proc.returncode,
                  "tail": (proc.stdout + proc.stderr)[-400:]}))
"""


def make_tree(root: Path, files: int, dup_fraction: float, seed: int = 1) -> int:
    rng = random.Random(seed)
    total = 0
    made: list[Path] = []
    for n in range(files):
        folder = root / f"d{n % 400:03d}" / f"s{(n // 400) % 50:02d}"
        if n % 400 == 0 or not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"file{n:07d}.dat"
        if made and rng.random() < dup_fraction:
            source = rng.choice(made[-2000:])
            path.write_bytes(source.read_bytes())
        else:
            size = min(int(rng.lognormvariate(8.3, 1.6)), 4 * 1024 * 1024)  # median ~4 KiB, long tail to 4 MiB
            path.write_bytes(rng.randbytes(max(size, 1)))
        total += path.stat().st_size
        made.append(path)
        if len(made) > 5000:
            made = made[-2500:]
        os.utime(path, (1_600_000_000 + n % 1000, 1_600_000_000 + n % 1000))
    return total


def hard_link_copy(src: Path, dst: Path, change_fraction: float, seed: int = 2) -> int:
    rng = random.Random(seed)
    changed = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if rng.random() < change_fraction:
            data = bytearray(path.read_bytes())
            if data:
                data[len(data) // 2] ^= 0xFF
            target.write_bytes(bytes(data))
            os.utime(target, (path.stat().st_atime, path.stat().st_mtime + 10))
            changed += 1
        else:
            os.link(path, target)
    return changed


def measure(*command: str) -> dict:
    out = subprocess.run([sys.executable, "-c", WRAPPER, *command], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=200_000)
    parser.add_argument("--dup-fraction", type=float, default=0.1)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="leave the generated data in place")
    args = parser.parse_args()

    work = args.dir / "duplio-scale-benchmark"
    if work.exists():
        shutil.rmtree(work)
    tree, other, repo, home = work / "tree", work / "tree2", work / "repo", work / "home"
    tree.mkdir(parents=True)
    other.mkdir()
    started = time.perf_counter()
    nbytes = make_tree(tree, args.files, args.dup_fraction)
    changed = hard_link_copy(tree, other, 0.01)
    print(f"generated {args.files:,} files, {nbytes / 1e9:.2f} GB, in {time.perf_counter() - started:.0f} s "
          f"({changed:,} changed in the second tree)")
    env = dict(os.environ, DUPLIO_HOME=str(home))
    os.environ.update(env)
    duplio = [sys.executable, "-m", "duplio", "--no-color"]

    results: dict[str, dict] = {}
    plan = [
        ("scan one tree for duplicates", [*duplio, "scan", str(tree), "--no-cache", "--top", "3"]),
        ("backup init", [*duplio, "backup", "init", str(repo)]),
        ("backup, first run", [*duplio, "backup", "run", str(tree), str(repo)]),
        ("backup, second run (nothing changed)", [*duplio, "backup", "run", str(tree), str(repo)]),
        ("backup check", [*duplio, "backup", "check", str(repo)]),
        ("diff two trees (1 % changed)", [*duplio, "diff", str(tree), str(other)]),
        ("sync dry run, first", [*duplio, "sync", str(tree), str(other), "--dry-run", "--on-conflict", "skip"]),
    ]
    for label, command in plan:
        result = measure(*command)
        results[label] = result
        flag = "" if result["exit"] in (0, 1) else f"  (exit {result['exit']}: {result['tail']!r})"
        print(f"{label:44s} {result['seconds']:8.1f} s   peak {result['rss_mib']:7.0f} MiB{flag}")
    repo_bytes = sum(p.stat().st_size for p in repo.rglob("*") if p.is_file())
    print(f"repository: {repo_bytes / 1e9:.2f} GB for {nbytes / 1e9:.2f} GB of data")
    print(f"machine: {platform.platform()}, {os.cpu_count()} cores, python {platform.python_version()}")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

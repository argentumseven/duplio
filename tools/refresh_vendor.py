#!/usr/bin/env python3
"""Rebuild the ``vendor/`` folder: the pure-Python libraries Duplio runs on, so the launchers need no installation.

Run this from a computer with internet access whenever the libraries should be updated:

    python tools/refresh_vendor.py

It downloads ready-made wheels of the libraries below (and what they depend on) with ``pip``, refuses to continue if
any of them contains compiled code (which would only work on one kind of computer), unpacks them into ``vendor/``,
and rewrites ``vendor/README.md``. Review the result with ``git diff --stat vendor`` and run the test suite.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"
REQUIREMENTS = ["click>=8.1", "rich>=13", "textual>=0.80"]
COMPILED = (".so", ".pyd", ".dll", ".dylib")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="duplio-vendor-") as tmp:
        target = Path(tmp) / "site"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-compile", "--only-binary=:all:", "--target", str(target), *REQUIREMENTS],
            check=True,
        )
        compiled = [p for p in target.rglob("*") if p.suffix in COMPILED]
        if compiled:
            print("These files are compiled code, which cannot be bundled for every computer:", file=sys.stderr)
            for path in compiled:
                print("  ", path.relative_to(target), file=sys.stderr)
            return 1
        shutil.rmtree(target / "bin", ignore_errors=True)
        for junk in list(target.rglob("__pycache__")):
            shutil.rmtree(junk, ignore_errors=True)
        for name in ("INSTALLER", "REQUESTED", "direct_url.json"):
            for path in target.rglob(name):
                path.unlink()
        rows = []
        for info in sorted(target.glob("*.dist-info")):
            meta = Parser().parsestr((info / "METADATA").read_text(encoding="utf-8"))
            license_name = meta.get("License-Expression") or meta.get("License") or _classifier_license(meta.get_all("Classifier") or [])
            rows.append((meta["Name"], meta["Version"], license_name or "see its dist-info folder", meta.get("Home-page") or ""))
        if VENDOR.exists():
            shutil.rmtree(VENDOR)
        shutil.copytree(target, VENDOR)
    (VENDOR / "README.md").write_text(_readme(rows), encoding="utf-8")
    print(f"vendor/ rebuilt with {len(rows)} libraries.")
    return 0


def _classifier_license(classifiers: list[str]) -> str:
    for line in classifiers:
        match = re.match(r"License :: (?:OSI Approved :: )?(.+)", line)
        if match:
            return match.group(1)
    return ""


def _readme(rows: list[tuple[str, str, str, str]]) -> str:
    table = "\n".join(f"| {name} | {version} | {lic} |" for name, version, lic, _home in rows)
    return f"""# Bundled libraries

These are the libraries Duplio runs on, copied here unchanged so that `Duplio.bat`, `Duplio.command` and `duplio.sh`
work on any computer that has Python 3.11 or newer, with nothing to install, download or compile.

Every one of them is pure Python (no compiled parts), so the same files work on Windows, macOS and Linux. Each keeps its
own licence file in its `*.dist-info` folder.

| Library | Version | Licence |
| --- | --- | --- |
{table}

Duplio itself lives in `../src`. Optional extras that need compiled code (Pillow for similar-image search,
`cryptography` for encrypted backups, `smbprotocol` for native `smb://`, `numpy`, `blake3`) are not bundled; Duplio
works without them and says so when a feature needs one. Install them with `pip` if wanted.

To rebuild this folder: `python tools/refresh_vendor.py` (needs internet access). Do not edit these files by hand.
"""


if __name__ == "__main__":
    raise SystemExit(main())

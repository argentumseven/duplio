# Bundled libraries

These are the libraries Duplio runs on, copied here unchanged so that `Duplio.bat`, `Duplio.command` and `duplio.sh`
work on any computer that has Python 3.11 or newer, with nothing to install, download or compile.

Every one of them is pure Python (no compiled parts), so the same files work on Windows, macOS and Linux. Each keeps its
own licence file in its `*.dist-info` folder.

| Library | Version | Licence |
| --- | --- | --- |
| click | 8.5.0 | BSD-3-Clause |
| linkify-it-py | 2.2.0 | MIT |
| markdown-it-py | 4.2.0 | MIT License |
| mdit-py-plugins | 0.6.1 | MIT License |
| mdurl | 0.1.2 | MIT License |
| platformdirs | 4.13.0 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| rich | 15.0.0 | MIT |
| textual | 8.2.8 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |

Duplio itself lives in `../src`. Optional extras that need compiled code (Pillow for similar-image search,
`cryptography` for encrypted backups, `smbprotocol` for native `smb://`, `numpy`, `blake3`) are not bundled; Duplio
works without them and says so when a feature needs one. Install them with `pip` if wanted.

To rebuild this folder: `python tools/refresh_vendor.py` (needs internet access). Do not edit these files by hand.

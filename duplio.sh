#!/bin/sh
# Duplio launcher for Linux and macOS.
#
# Runs Duplio straight from this folder. Nothing to install, nothing to compile, nothing to download:
# everything Duplio needs is in the "src" and "vendor" folders next to this file. The only thing the
# computer must have is Python 3.11 or newer.
#
#   ./duplio.sh                 open Duplio's window (two folders side by side, with pictures)
#   ./duplio.sh scan ~/Pictures run a command (./duplio.sh --help lists them)
#
# To use it from anywhere, link it into a folder on your PATH:  ln -s "$PWD/duplio.sh" ~/.local/bin/duplio

# ---- find this folder, following links so a shortcut or symlink works too
self=$0
while [ -h "$self" ]; do
  dir=$(cd "$(dirname "$self")" && pwd -P)
  target=$(readlink "$self")
  case $target in
    /*) self=$target ;;
    *) self=$dir/$target ;;
  esac
done
here=$(cd "$(dirname "$self")" 2>/dev/null && pwd -P) || {
  echo "Duplio: cannot work out which folder it is in." >&2
  exit 1
}

if [ ! -f "$here/src/duplio/__init__.py" ]; then
  cat >&2 <<EOF

Duplio cannot find its program files next to this launcher ($here).
If you opened this from inside a ZIP file, extract the whole ZIP first, then start duplio.sh
from the new folder.

EOF
  exit 1
fi

# ---- find a Python that is new enough
is_new_enough() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

# Duplio's window needs Tk, which some Pythons lack (a Linux python3 without python3-tk, Homebrew's Python). The text commands
# do not need it, so: prefer a Python that has Tk, and settle for any new-enough one.
has_tk() {
  "$1" -c 'import tkinter' >/dev/null 2>&1
}

# Every Python that might do, one path per line.
candidates() {
  for name in python3.15 python3.14 python3.13 python3.12 python3.11 python3 python; do
    found=$(command -v "$name" 2>/dev/null) || continue
    echo "$found"
  done
  # Places installers put Python that a plain terminal does not always have on its PATH (macOS in particular).
  for dir in /opt/homebrew/bin /usr/local/bin /Library/Frameworks/Python.framework/Versions/Current/bin "$HOME/.local/bin" /usr/bin; do
    for name in python3.15 python3.14 python3.13 python3.12 python3.11 python3; do
      [ -x "$dir/$name" ] && echo "$dir/$name"
    done
  done
}

find_python() {
  if [ -n "$DUPLIO_PYTHON" ]; then
    is_new_enough "$DUPLIO_PYTHON" && { echo "$DUPLIO_PYTHON"; return 0; }
    echo "Duplio: DUPLIO_PYTHON ($DUPLIO_PYTHON) is not Python 3.11 or newer; looking for another." >&2
  fi
  list=$(candidates)
  fallback=""
  while IFS= read -r found; do
    [ -n "$found" ] || continue
    is_new_enough "$found" || continue
    if has_tk "$found"; then
      echo "$found"
      return 0
    fi
    [ -n "$fallback" ] || fallback=$found
  done <<LIST
$list
LIST
  [ -n "$fallback" ] && { echo "$fallback"; return 0; }
  return 1
}

explain_missing_python() {
  cat >&2 <<'EOF'

Duplio needs Python 3.11 or newer, and this computer does not have it yet.
Python is a free, widely used program. Duplio itself installs nothing else.

EOF
  case $(uname -s 2>/dev/null) in
    Darwin)
      cat >&2 <<'EOF'
On a Mac:
  1. Open https://www.python.org/downloads/ in your browser.
  2. Download the macOS installer and open it (keep clicking Continue).
  3. Start Duplio again.
(If you use Homebrew you can instead run:  brew install python)
EOF
      ;;
    *)
      if command -v apt-get >/dev/null 2>&1; then
        cmd="sudo apt install python3"
      elif command -v dnf >/dev/null 2>&1; then
        cmd="sudo dnf install python3"
      elif command -v pacman >/dev/null 2>&1; then
        cmd="sudo pacman -S python"
      elif command -v zypper >/dev/null 2>&1; then
        cmd="sudo zypper install python3"
      elif command -v apk >/dev/null 2>&1; then
        cmd="sudo apk add python3"
      else
        cmd=""
      fi
      if [ -n "$cmd" ]; then
        printf 'Install it with:\n  %s\nthen start Duplio again.\n' "$cmd" >&2
      else
        echo "Install Python 3.11 or newer from https://www.python.org/downloads/ and start Duplio again." >&2
      fi
      ;;
  esac
  echo >&2
}

py=$(find_python) || {
  explain_missing_python
  exit 1
}

# ---- run Duplio from this folder: its own code first, then the bundled libraries
PYTHONPATH="$here/src:$here/vendor${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
PYTHONUTF8=1
export PYTHONUTF8

exec "$py" -m duplio "$@"

#!/bin/sh
# Double-click this file on a Mac to start Duplio. (First time only: right-click it, choose Open, then Open again --
# macOS asks once because the file came from the internet.)
#
# It just runs duplio.sh in this folder, and keeps the window open if something went wrong so you can read why.

cd "$(dirname "$0")" || exit 1
sh ./duplio.sh "$@"
status=$?

if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then
  echo
  echo "Duplio stopped with an error (code $status). The messages above say why."
  printf "Press Return to close this window. "
  read -r _
fi
exit "$status"

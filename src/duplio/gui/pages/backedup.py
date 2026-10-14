"""Is it backed up? Everything in A, checked against B."""

from __future__ import annotations

from duplio.compare.diff import DiffResult
from duplio.gui.model import RowKind, backup_verdict
from duplio.gui.pages.pairpage import CHIP_COLORS, PairPage
from duplio.gui.style import PALETTE


class BackedUpPage(PairPage):
    title = "Is it backed up?"
    subtitle = ("Check that every file in A has a safe copy in B, whatever the copy is called. The files that are missing are shown "
                "with their pictures, and one button copies them across.")
    a_title, a_hint = "Check this", "your phone, camera card or the folder you work in"
    b_title, b_hint = "Against this", "your backup drive or backup folder"
    run_text = "Check the backup"
    allow_reverse = False
    recursion = ("Include sub-folders of A", "Include sub-folders of the backup")
    names = ("A", "backup")
    side_words = ("A", "the backup")
    chips = (
        ("missing", "Not in the backup", (RowKind.ONLY_A,), PALETTE["bad"]),
        ("changed", "Only a different version is there", (RowKind.CHANGED,), CHIP_COLORS[RowKind.CHANGED]),
        ("unreadable", "Could not be checked", (RowKind.UNREADABLE,), CHIP_COLORS[RowKind.UNREADABLE]),
        ("safe", "Safe", (RowKind.SAME, RowKind.MOVED), PALETTE["ok"]),
        ("extra", "Only in the backup", (RowKind.ONLY_B,), CHIP_COLORS[RowKind.ONLY_B]),
    )

    def placeholder_text(self) -> str:
        return ("Choose what to check and where its backup is, then press the button. By default only the files directly inside each "
                "folder are looked at; tick a box to include the sub-folders of that side too. Nothing is changed by checking.")

    def banner_text(self, diff: DiffResult) -> tuple[str, str, str]:
        verdict = backup_verdict(diff, "A", "the backup")
        kind = "bad" if not verdict.ok else "warn" if verdict.partial else "ok"  # amber: safe, but not everything was looked at
        return verdict.headline, verdict.detail, kind

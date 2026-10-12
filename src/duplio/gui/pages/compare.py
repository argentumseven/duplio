"""Compare two folders: what is in A, what is in B, what is the same whatever it is called."""

from __future__ import annotations

from duplio.compare.diff import DiffResult, Kind
from duplio.gui.model import RowKind
from duplio.gui.pages.pairpage import CHIP_COLORS, PairPage
from duplio.util.fmt import human_size, plural


class ComparePage(PairPage):
    title = "Compare two folders"
    subtitle = ("Folder A on the left, folder B on the right. Files are matched by what is inside them, so a renamed or moved "
                "photo is not a difference. Looking changes nothing; you choose what to copy.")
    a_title, a_hint = "Folder A", "for example your phone or the folder you work in"
    b_title, b_hint = "Folder B", "for example a backup drive or another computer"
    run_text = "Compare A and B"
    chips = (
        ("only-a", "Only in A", (RowKind.ONLY_A,), CHIP_COLORS[RowKind.ONLY_A]),
        ("only-b", "Only in B", (RowKind.ONLY_B,), CHIP_COLORS[RowKind.ONLY_B]),
        ("changed", "Different", (RowKind.CHANGED,), CHIP_COLORS[RowKind.CHANGED]),
        ("moved", "Renamed or moved", (RowKind.MOVED,), CHIP_COLORS[RowKind.MOVED]),
        ("unreadable", "Could not be read", (RowKind.UNREADABLE,), CHIP_COLORS[RowKind.UNREADABLE]),
        ("same", "Identical", (RowKind.SAME,), CHIP_COLORS[RowKind.SAME]),
    )

    def banner_text(self, diff: DiffResult) -> tuple[str, str, str]:
        only_a, only_b = diff.count(Kind.ONLY, 0), diff.count(Kind.ONLY, 1)
        changed = diff.count(Kind.CHANGED, 0)
        unreadable = sum(1 for i in diff.items if i.kind is Kind.UNREADABLE and i.side == 0)
        total = len(diff.inventory.entries)
        safe = diff.count(Kind.SAME, 0) + diff.count(Kind.MOVED, 0)
        detail = f"{plural(safe, 'file')} of A {'has' if safe == 1 else 'have'} an identical copy in B."
        if not total:
            return "Both folders are empty.", "", "ok"
        if diff.identical_layout:
            return "The two folders are identical.", f"{plural(total // 2 if total else 0, 'file')} each, same names, same contents.", "ok"
        if diff.identical_content:
            return "Same files, arranged differently.", "Every file in A has an identical copy in B and the other way round, but some have other names or are in other folders.", "ok"
        parts = []
        if only_a:
            parts.append(f"{plural(only_a, 'file')} only in A ({human_size(diff.size(Kind.ONLY, 0))})")
        if only_b:
            parts.append(f"{plural(only_b, 'file')} only in B ({human_size(diff.size(Kind.ONLY, 1))})")
        if changed:
            parts.append(f"{plural(changed, 'file')} differ between the two")
        if unreadable:
            parts.append(f"{plural(unreadable, 'file')} could not be read")
        return "The folders are not the same.", "; ".join(parts) + ". " + detail, "warn"


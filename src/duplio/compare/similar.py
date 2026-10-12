"""Perceptually similar images (resized, re-encoded, lightly edited copies).

Uses a 64-bit difference hash (dHash) computed on a tiny greyscale thumbnail.
JPEGs are decoded with ``Image.draft`` at 1/8 scale, which makes hashing a
12-megapixel photo about as cheap as reading its header. Neighbours within a
Hamming radius are found with a BK-tree instead of comparing every pair.

Grouping is *star-shaped*: each group is one representative (the largest
image) plus images close to *it*. Transitive chaining (A≈B, B≈C, so A≈C) is
deliberately avoided because it produces groups of images that do not
actually look alike.

Similar groups are advisory: Duplio never acts on them automatically.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from duplio.compare.model import DuplicateGroup, EngineStats, SimilarGroup
from duplio.compare.scheduler import Job, run_jobs
from duplio.events import CancelToken, ProgressSink
from duplio.hashing.cache import HashCache
from duplio.preview.service import PreviewItem, PreviewService, Unavailable
from duplio.scan.filters import IMAGE_EXTENSIONS, extension
from duplio.scan.inventory import FileEntry, Inventory

DHASH_KIND = "dhash:v1"
THUMB_DHASH_KIND = "dhash:thumb:v1"  # from a small embedded preview; kept apart because it is a slightly different hash
MAX_IMAGE_BYTES = 256 * 1024 * 1024


def have_pillow() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _hash_pixels(pixels: bytes) -> int:
    value = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            value = (value << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return value


def dhash(handle) -> tuple[int, int, int]:  # type: ignore[no-untyped-def]
    """``(hash, width, height)`` of an image file object."""
    from PIL import Image, ImageOps

    with Image.open(handle) as img:
        width, height = img.size
        try:
            img.draft("L", (64, 64))  # JPEG: decode at reduced scale
        except Exception:
            pass
        img = ImageOps.exif_transpose(img) or img
        small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = small.tobytes()  # one byte per pixel in mode 'L'
    return _hash_pixels(pixels), width, height


_ORIENTATION = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE", 6: "ROTATE_270",
                7: "TRANSVERSE", 8: "ROTATE_90"}


def dhash_thumbnail(data: bytes, orientation: int, original: tuple[int, int] | None) -> tuple[int, int, int]:
    """dHash from a small stored thumbnail instead of the whole picture (so a slow drive is not read again).

    The pixel size reported is the picture's own when its header said, otherwise 0 x 0 ("unknown").
    """
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        method = _ORIENTATION.get(orientation)
        if method:
            img = img.transpose(getattr(Image.Transpose, method))
        small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = small.tobytes()
    width, height = original if original else (0, 0)
    if original and orientation in (5, 6, 7, 8):
        width, height = height, width
    return _hash_pixels(pixels), width, height


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class BKTree:
    """Metric tree over Hamming distance."""

    def __init__(self) -> None:
        self._root: tuple[int, list[int], dict[int, object]] | None = None

    def add(self, value: int, item: int) -> None:
        if self._root is None:
            self._root = (value, [item], {})
            return
        node = self._root
        while True:
            dist = hamming(value, node[0])
            if dist == 0:
                node[1].append(item)
                return
            child = node[2].get(dist)
            if child is None:
                node[2][dist] = (value, [item], {})
                return
            node = child  # type: ignore[assignment]

    def query(self, value: int, radius: int) -> Iterator[tuple[int, int]]:
        """Yield ``(item, distance)`` for all items within ``radius``."""
        if self._root is None:
            return
        stack = [self._root]
        while stack:
            node = stack.pop()
            dist = hamming(value, node[0])
            if dist <= radius:
                for item in node[1]:
                    yield item, dist
            for edge, child in node[2].items():
                if dist - radius <= edge <= dist + radius:
                    stack.append(child)  # type: ignore[arg-type]


def find_similar_images(
    inventory: Inventory,
    exact_groups: list[DuplicateGroup],
    *,
    threshold: int,
    cache: HashCache,
    progress: ProgressSink,
    cancel: CancelToken,
    stats: EngineStats,
    previews: PreviewService | None = None,
    full_read: bool = False,
) -> list[SimilarGroup]:
    """Group visually similar images.

    On slow, remote and cloud drives the hash is taken from the picture's small embedded preview (a few kilobytes read, kept
    in the preview cache) rather than from the whole image, unless ``full_read``: reading a photo library over a slow link
    just to fingerprint it is what makes these scans take hours. Images without a small preview there are not compared, and
    a message says how many.
    """
    if not have_pillow():
        progress.message("Similar-image search needs Pillow: pip install 'duplio[media]'", level="warning")
        return []
    # Exact copies are already reported; keep one representative of each.
    redundant = {m.id for g in exact_groups for m in g.members[1:]}
    candidates = [
        e
        for e in inventory.entries
        if e.id not in redundant
        and extension(e.rel) in IMAGE_EXTENSIONS
        and 0 < e.size <= MAX_IMAGE_BYTES
    ]
    progress.stage("Fingerprinting images", total_items=len(candidates))
    hashes: dict[int, tuple[int, int, int]] = {}
    lock = threading.Lock()
    jobs: list[Job] = []

    skipped = {"count": 0}
    for entry in candidates:
        root = inventory.roots[entry.root]
        use_preview = previews is not None and not full_read and root.caps.read_cost > 0
        kind = THUMB_DHASH_KIND if use_preview else DHASH_KIND
        cached = cache.get(root.backend.id, entry.path, kind, entry.size, entry.mtime_ns)
        if cached is not None:
            h, w, ht = (int(x, 16) for x in cached.split(":"))
            hashes[entry.id] = (h, w, ht)
            progress.advance(items=1)
            continue

        def job(entry: FileEntry = entry, use_preview: bool = use_preview, kind: str = kind) -> None:
            root = inventory.roots[entry.root]
            read = 0
            try:
                if use_preview:
                    assert previews is not None
                    item = PreviewItem(root.backend, entry.path, entry.size, entry.mtime_ns, entry.rel, entry.offline)
                    try:
                        preview = previews.get(item, cancel=cancel)
                    except Unavailable:
                        with lock:
                            skipped["count"] += 1
                        progress.advance(items=1)
                        return
                    value = dhash_thumbnail(preview.thumb.data, preview.thumb.orientation, preview.thumb.original)
                    read = preview.bytes_read
                else:
                    with root.backend.open_read(entry.path) as handle:
                        seekable = getattr(handle, "seekable", None)
                        if seekable is not None and seekable():
                            value = dhash(handle)
                        else:  # device streams: buffer the image so Pillow can seek
                            import io

                            value = dhash(io.BytesIO(handle.read()))
                    read = entry.size
            except Exception:
                # Not an image Pillow understands (or unreadable): simply not compared.
                progress.advance(items=1)
                return
            with lock:
                hashes[entry.id] = value
            cache.put(
                root.backend.id,
                entry.path,
                kind,
                entry.size,
                entry.mtime_ns,
                f"{value[0]:x}:{value[1]:x}:{value[2]:x}",
                mtime_resolution_ns=root.caps.mtime_resolution_ns,
            )
            progress.advance(items=1, nbytes=read)

        caps = root.caps
        jobs.append(Job(root.backend.device_key(entry.path), caps.max_parallel_reads, job))
    run_jobs(jobs, cancel=cancel)
    if skipped["count"]:
        progress.message(
            f"{skipped['count']:,} images on slow, remote or cloud drives have no small preview inside them and were not "
            "compared (--similar-full-read reads them in full).",
            level="warning",
        )

    by_id = {e.id: e for e in candidates}
    tree = BKTree()
    for entry_id, (value, _, _) in hashes.items():
        tree.add(value, entry_id)
    order = sorted(hashes, key=lambda i: (-(hashes[i][1] * hashes[i][2]), -by_id[i].size, by_id[i].path))
    assigned: set[int] = set()
    groups: list[SimilarGroup] = []
    for entry_id in order:
        if entry_id in assigned:
            continue
        neighbours = sorted(
            ((other, dist) for other, dist in tree.query(hashes[entry_id][0], threshold) if other != entry_id and other not in assigned),
            key=lambda x: (x[1], by_id[x[0]].path),
        )
        if not neighbours:
            continue
        assigned.add(entry_id)
        members = [by_id[entry_id]]
        distances = [0]
        for other, dist in neighbours:
            assigned.add(other)
            members.append(by_id[other])
            distances.append(dist)
        groups.append(
            SimilarGroup(
                id=len(groups) + 1,
                members=members,
                distances=distances,
                dimensions=[(hashes[m.id][1], hashes[m.id][2]) for m in members],
            )
        )
    return groups

"""What has to be signed, and nothing weaker than the file's own first bytes.

Extension suffixes were the obvious alternative and are wrong here: the tree
carries `.so`, `.dylib`, a suffix-less `python3.12`, and a `libportaudio.dylib`
that arrives inside a data directory rather than beside a module. A rule made of
suffixes would have to be right about all of that, and would go stale the next
time the lock is regenerated. The first four bytes cannot go stale.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

#: The Mach-O and universal-binary magic numbers, as they appear on disk.
#: Both byte orders of each, because the constant is defined in the file's own
#: endianness and macOS has shipped both. `0xcffaedfe` is what everything in a
#: current arm64 tree actually starts with; the rest cost nothing to accept and
#: mean the rule is about the format rather than about today's toolchain.
MACH_O_MAGIC: frozenset[bytes] = frozenset(
    {
        b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64, little-endian
        b"\xce\xfa\xed\xfe",  # MH_MAGIC, little-endian
        b"\xfe\xed\xfa\xcf",  # MH_CIGAM_64
        b"\xfe\xed\xfa\xce",  # MH_CIGAM
        b"\xca\xfe\xba\xbe",  # FAT_MAGIC
        b"\xbe\xba\xfe\xca",  # FAT_CIGAM
        b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
        b"\xbf\xba\xfe\xca",  # FAT_CIGAM_64
    }
)

#: How much of a file has to be read to answer the question.
MAGIC_BYTES = 4


def is_mach_o(header: bytes) -> bool:
    """Do these opening bytes say Mach-O? A pure function, so it needs no disk."""
    return header[:MAGIC_BYTES] in MACH_O_MAGIC


def opening_bytes(path: Path) -> bytes:
    """The first four bytes of a file, or none at all if it is shorter."""
    with path.open("rb") as handle:
        return handle.read(MAGIC_BYTES)


def signable(root: Path) -> tuple[Path, ...]:
    """Every Mach-O file under ``root``, in a deterministic order.

    **Symlinks are not followed and are never returned.** A bundled interpreter
    tree is full of them — `bin/python3` points at `bin/python3.12` — and signing
    through one would sign the same file twice under two names, which is how a
    signature ends up replacing the one just made.

    The order is sorted rather than whatever the filesystem offers, because the
    signing order is derived from this and a plan that reordered itself between
    two runs could not be asserted about at all.
    """
    return tuple(sorted(_walk(root)))


def _walk(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            header = opening_bytes(path)
        except OSError:
            # A file that cannot be opened cannot be signed either, and the
            # signing step will say so with `codesign`'s own words rather than
            # this walk inventing a second complaint about the same file.
            continue
        if is_mach_o(header):
            yield path

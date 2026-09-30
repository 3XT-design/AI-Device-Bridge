"""Create a deterministic large file for manual transfer throughput testing."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def create_file(path: Path, size_bytes: int) -> str:
    if size_bytes < 1:
        raise ValueError("size_bytes must be positive")
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    block = bytes(4 * 1024 * 1024)
    remaining = size_bytes
    with path.open("xb") as output:
        while remaining:
            chunk = block[: min(len(block), remaining)]
            output.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path.home() / "Downloads" / "ai-device-bridge-test-1GiB.bin",
    )
    parser.add_argument("--size-gib", type=float, default=1.0)
    args = parser.parse_args()
    try:
        size_bytes = int(args.size_gib * 1024**3)
        digest = create_file(args.output, size_bytes)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"File: {args.output.expanduser().resolve()}")
    print(f"Size: {size_bytes} bytes")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

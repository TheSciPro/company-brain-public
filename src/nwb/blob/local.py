"""LocalBlobStore: content-addressed files under a root folder. Same bytes -> same URL (dedupe)."""

from __future__ import annotations

import hashlib
from pathlib import Path


class LocalBlobStore:
    scheme = "blob+sha256://"

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)  # atomic on POSIX
        return self.scheme + digest

    def get(self, url: str) -> bytes:
        if not url.startswith(self.scheme):
            raise ValueError(f"not a local blob url: {url}")
        return self._path(url.removeprefix(self.scheme)).read_bytes()

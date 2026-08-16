from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredObject:
    body: bytes
    content_type: str


class ObjectStorage(Protocol):
    async def ensure_ready(self) -> None: ...

    async def create_upload_url(
        self, object_key: str, *, content_type: str, expires_seconds: int
    ) -> str: ...

    async def create_download_url(self, object_key: str, *, expires_seconds: int) -> str: ...

    async def read(self, object_key: str) -> StoredObject: ...

    async def write(self, object_key: str, body: bytes, *, content_type: str) -> None: ...

    async def delete(self, object_key: str) -> None: ...


class ObjectNotFoundError(Exception):
    pass

from __future__ import annotations

import asyncio
from typing import Any, cast

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.domain.artifacts import ObjectNotFoundError, ObjectStorage, StoredObject


class MemoryObjectStorage:
    """Process-local storage used only by isolated tests."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}

    async def ensure_ready(self) -> None:
        return None

    async def create_upload_url(
        self, object_key: str, *, content_type: str, expires_seconds: int
    ) -> str:
        return f"memory://upload/{object_key}"

    async def create_download_url(self, object_key: str, *, expires_seconds: int) -> str:
        if object_key not in self.objects:
            raise ObjectNotFoundError(object_key)
        return f"memory://download/{object_key}"

    async def read(self, object_key: str) -> StoredObject:
        try:
            return self.objects[object_key]
        except KeyError as exc:
            raise ObjectNotFoundError(object_key) from exc

    async def write(self, object_key: str, body: bytes, *, content_type: str) -> None:
        self.objects[object_key] = StoredObject(body=body, content_type=content_type)

    async def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)


class S3ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        client_options = {
            "region_name": settings.object_storage_region,
            "aws_access_key_id": settings.object_storage_access_key,
            "aws_secret_access_key": settings.object_storage_secret_key,
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }
        self._client: Any = boto3.client(
            "s3", endpoint_url=settings.object_storage_endpoint_url, **client_options
        )
        public_endpoint = (
            settings.object_storage_public_endpoint_url or settings.object_storage_endpoint_url
        )
        self._signing_client: Any = boto3.client(
            "s3", endpoint_url=public_endpoint, **client_options
        )
        self._bucket = settings.object_storage_bucket
        self._ready = False

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        await asyncio.to_thread(self._ensure_bucket)
        self._ready = True

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            self._client.create_bucket(Bucket=self._bucket)

    async def create_upload_url(
        self, object_key: str, *, content_type: str, expires_seconds: int
    ) -> str:
        await self.ensure_ready()
        return cast(
            str,
            self._signing_client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": object_key,
                    "ContentType": content_type,
                },
                ExpiresIn=expires_seconds,
            ),
        )

    async def create_download_url(self, object_key: str, *, expires_seconds: int) -> str:
        return cast(
            str,
            self._signing_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": object_key},
                ExpiresIn=expires_seconds,
            ),
        )

    async def read(self, object_key: str) -> StoredObject:
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=object_key
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectNotFoundError(object_key) from exc
            raise
        body = await asyncio.to_thread(response["Body"].read)
        return StoredObject(body=body, content_type=str(response.get("ContentType", "")))

    async def write(self, object_key: str, body: bytes, *, content_type: str) -> None:
        await self.ensure_ready()
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=object_key,
            Body=body,
            ContentType=content_type,
        )

    async def delete(self, object_key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=object_key)


_memory_storage = MemoryObjectStorage()


def build_object_storage(settings: Settings) -> ObjectStorage:
    if settings.object_storage_provider == "memory":
        return _memory_storage
    return S3ObjectStorage(settings)

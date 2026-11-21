"""Content-addressed artifact storage: bytes in S3, metadata in `artifacts` (RLS-scoped).

Keys: orgs/<org>/workflows/<workflow|_unscoped>/<kind>/<sha256>
  * Same bytes, same scope -> same key -> written once (idempotent puts).
  * Scoping by org means no cross-tenant de-duplication, which would leak whether
    another tenant holds a given file.
S3 verifies the SHA-256 on upload (ChecksumSHA256); reads re-verify it.
boto3 is synchronous, so calls run in worker threads.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from aieos.core.errors import ConflictError, NotFoundError
from aieos.core.ids import uuid7
from aieos.core.logging import get_logger
from aieos.core.settings import StorageSettings
from aieos.persistence import repositories as repo
from aieos.persistence.db import Database

log = get_logger(__name__)

ARTIFACT_KINDS = frozenset(
    {
        "request",
        "requirements_spec",
        "architecture_design",
        "plan",
        "patch",
        "test_report",
        "security_report",
        "review_report",
        "execution_log",
        "agent_output",
        "tool_result",
        "other",
    }
)


class ArtifactIntegrityError(RuntimeError):
    """Stored bytes do not match the recorded SHA-256 or size."""


class ArtifactTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    url: str
    fields: dict[str, str]
    key: str
    max_bytes: int
    expires_in: int


def build_s3_client(settings: StorageSettings) -> Any:
    kwargs: dict[str, Any] = {
        "region_name": settings.s3_region,
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        ),
    }
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    if settings.s3_access_key_id and settings.s3_secret_access_key:
        kwargs["aws_access_key_id"] = settings.s3_access_key_id.get_secret_value()
        kwargs["aws_secret_access_key"] = settings.s3_secret_access_key.get_secret_value()
    return boto3.client("s3", **kwargs)


def _scope(org_id: UUID, workflow_id: UUID | None) -> str:
    return f"orgs/{org_id}/workflows/{workflow_id or '_unscoped'}"


class ArtifactStore:
    def __init__(
        self,
        db: Database,
        s3: Any,
        *,
        bucket: str,
        kms_key_id: str | None = None,
        max_bytes: int = 100 * 1024 * 1024,
        presign_ttl_seconds: int = 300,
    ) -> None:
        self._db = db
        self._s3 = s3
        self._bucket = bucket
        self._kms_key_id = kms_key_id
        self._max_bytes = max_bytes
        self._ttl = presign_ttl_seconds

    @classmethod
    def from_settings(cls, db: Database, settings: StorageSettings) -> ArtifactStore:
        return cls(
            db,
            build_s3_client(settings),
            bucket=settings.s3_artifact_bucket,
            kms_key_id=settings.s3_kms_key_id,
            max_bytes=settings.artifact_max_bytes,
            presign_ttl_seconds=settings.presign_ttl_seconds,
        )

    @staticmethod
    def object_key(org_id: UUID, workflow_id: UUID | None, kind: str, sha256: str) -> str:
        return f"{_scope(org_id, workflow_id)}/{kind}/{sha256}"

    # ------------------------------------------------------------------ write
    async def put(
        self,
        org_id: UUID,
        data: bytes,
        *,
        kind: str,
        content_type: str,
        workflow_id: UUID | None = None,
        classification: str = "internal",
        retention_class: str = "standard",
    ) -> repo.ArtifactRow:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact kind {kind!r}")
        if len(data) > self._max_bytes:
            raise ArtifactTooLargeError(f"artifact is {len(data)} bytes; limit is {self._max_bytes}")
        digest = hashlib.sha256(data).digest()
        sha = digest.hex()
        key = self.object_key(org_id, workflow_id, kind, sha)
        if not await asyncio.to_thread(self._exists, key):
            await asyncio.to_thread(self._put_object, key, data, digest, content_type, classification, retention_class)
        async with self._db.tenant(org_id) as conn:
            row = await repo.upsert_artifact(
                conn,
                org_id=org_id,
                workflow_id=workflow_id,
                kind=kind,
                s3_bucket=self._bucket,
                s3_key=key,
                sha256=sha,
                size_bytes=len(data),
                content_type=content_type,
                classification=classification,
                retention_class=retention_class,
            )
        log.info("artifact.stored", artifact_id=str(row.id), kind=kind, size_bytes=len(data))
        return row

    def _exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def _put_object(
        self, key: str, data: bytes, digest: bytes, content_type: str, classification: str, retention_class: str
    ) -> None:
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "ChecksumSHA256": base64.b64encode(digest).decode(),
            "Tagging": urlencode({"retention": retention_class, "classification": classification}),
        }
        if self._kms_key_id:
            params.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=self._kms_key_id)
        self._s3.put_object(**params)

    # ------------------------------------------------------------------ read
    async def get_bytes(self, org_id: UUID, artifact_id: UUID) -> bytes:
        async with self._db.tenant(org_id) as conn:
            row = await repo.get_artifact(conn, artifact_id)
        data: bytes = await asyncio.to_thread(self._read, row.s3_key)
        if len(data) != row.size_bytes or hashlib.sha256(data).hexdigest() != row.sha256:
            log.error("artifact.integrity_failure", artifact_id=str(artifact_id))
            raise ArtifactIntegrityError(f"artifact {artifact_id} failed its integrity check")
        return data

    def _read(self, key: str) -> bytes:
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                raise ArtifactIntegrityError(f"object {key} is missing") from None
            raise
        body: bytes = obj["Body"].read(self._max_bytes + 1)
        return body

    async def presign_download(self, org_id: UUID, artifact_id: UUID, *, ttl_seconds: int | None = None) -> str:
        async with self._db.tenant(org_id) as conn:
            row = await repo.get_artifact(conn, artifact_id)  # RLS: other tenants get NotFoundError
        url: str = self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": row.s3_key}, ExpiresIn=self._clamp(ttl_seconds)
        )
        return url

    # ------------------------------------------------------------------ sandbox uploads
    def presign_upload(
        self, org_id: UUID, workflow_id: UUID, *, max_bytes: int | None = None, ttl_seconds: int | None = None
    ) -> PresignedUpload:
        """A one-object POST policy for a sandbox: fixed key, size-limited, short-lived.
        The sandbox never sees credentials; the gateway later calls `finalize_upload`."""
        limit = min(max_bytes or self._max_bytes, self._max_bytes)
        key = f"{_scope(org_id, workflow_id)}/uploads/{uuid7()}"
        fields: dict[str, str] = {}
        conditions: list[Any] = [["content-length-range", 1, limit]]
        if self._kms_key_id:
            fields.update(
                {
                    "x-amz-server-side-encryption": "aws:kms",
                    "x-amz-server-side-encryption-aws-kms-key-id": self._kms_key_id,
                }
            )
            conditions += [
                {"x-amz-server-side-encryption": "aws:kms"},
                {"x-amz-server-side-encryption-aws-kms-key-id": self._kms_key_id},
            ]
        ttl = self._clamp(ttl_seconds)
        post = self._s3.generate_presigned_post(
            Bucket=self._bucket, Key=key, Fields=fields or None, Conditions=conditions, ExpiresIn=ttl
        )
        return PresignedUpload(url=post["url"], fields=dict(post["fields"]), key=key, max_bytes=limit, expires_in=ttl)

    async def finalize_upload(
        self, org_id: UUID, workflow_id: UUID, upload_key: str, *, kind: str, content_type: str
    ) -> repo.ArtifactRow:
        """Move a sandbox upload to its content-addressed key and record it."""
        expected_prefix = f"{_scope(org_id, workflow_id)}/uploads/"
        if not upload_key.startswith(expected_prefix) or "/" in upload_key[len(expected_prefix) :]:
            raise ConflictError("upload key does not belong to this workflow")
        try:
            data = await asyncio.to_thread(self._read, upload_key)
        except ArtifactIntegrityError:
            raise NotFoundError("upload not found") from None
        if len(data) > self._max_bytes:
            raise ArtifactTooLargeError("upload exceeds the artifact size limit")
        row = await self.put(
            org_id,
            data,
            kind=kind,
            content_type=content_type,
            workflow_id=workflow_id,
            retention_class="ephemeral" if kind == "execution_log" else "standard",
        )
        await asyncio.to_thread(self._s3.delete_object, Bucket=self._bucket, Key=upload_key)
        return row

    def _clamp(self, ttl_seconds: int | None) -> int:
        return max(1, min(ttl_seconds or self._ttl, self._ttl))

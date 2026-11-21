"""Artifact store against real PostgreSQL (RLS) and in-process S3 (moto)."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid

import pytest
from moto import mock_aws

from aieos.artifacts.store import ArtifactIntegrityError, ArtifactStore, ArtifactTooLargeError, build_s3_client
from aieos.cli import admin
from aieos.core.errors import ConflictError, NotFoundError
from aieos.core.settings import StorageSettings
from aieos.persistence.db import Database
from tests.integration.conftest import seed_workflow

BUCKET = "aieos-artifacts-test"


@pytest.fixture(scope="module")
def orgs(provisioned_db, published_agents):
    owner = provisioned_db.owner_conninfo
    a = admin.create_org(owner, slug="art-a", name="A", owner_sub="art-owner-a", owner_email="o@art-a.test")
    b = admin.create_org(owner, slug="art-b", name="B", owner_sub="art-owner-b", owner_email="o@art-b.test")
    return a, b, seed_workflow(owner, a)["workflow"]


@pytest.fixture
def s3():
    with mock_aws():
        # The production client builder, so tests sign requests exactly as the service does.
        client = build_s3_client(StorageSettings(s3_artifact_bucket=BUCKET, _env_file=None))
        client.create_bucket(Bucket=BUCKET)
        yield client


def run(provisioned_db, s3, fn, **store_kwargs):
    async def main():
        db = Database(provisioned_db.app_async_url, pool_size=1, max_overflow=0)
        try:
            return await fn(ArtifactStore(db, s3, bucket=BUCKET, **store_kwargs))
        finally:
            await db.dispose()

    return asyncio.run(main())


def test_put_is_content_addressed_and_idempotent(provisioned_db, s3, orgs):
    org_a, _, wf = orgs

    async def scenario(store):
        first = await store.put(org_a, b"diff --git a b", kind="patch", content_type="text/x-diff", workflow_id=wf)
        second = await store.put(org_a, b"diff --git a b", kind="patch", content_type="text/x-diff", workflow_id=wf)
        return first, second, await store.get_bytes(org_a, first.id)

    first, second, data = run(provisioned_db, s3, scenario)
    assert first.id == second.id and data == b"diff --git a b"
    assert first.s3_key == f"orgs/{org_a}/workflows/{wf}/patch/{first.sha256}"
    assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 1
    tags = {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=BUCKET, Key=first.s3_key)["TagSet"]}
    assert tags == {"retention": "standard", "classification": "internal"}


def test_other_tenant_cannot_read_or_presign(provisioned_db, s3, orgs):
    org_a, org_b, _ = orgs

    async def scenario(store):
        art = await store.put(org_a, b"secret spec", kind="requirements_spec", content_type="application/json")
        with pytest.raises(NotFoundError):
            await store.get_bytes(org_b, art.id)
        with pytest.raises(NotFoundError):
            await store.presign_download(org_b, art.id)

    run(provisioned_db, s3, scenario)


def test_tampered_object_is_detected(provisioned_db, s3, orgs):
    org_a, _, _ = orgs

    async def scenario(store):
        art = await store.put(org_a, b"original report", kind="test_report", content_type="text/plain")
        s3.put_object(Bucket=BUCKET, Key=art.s3_key, Body=b"forged report!!")
        with pytest.raises(ArtifactIntegrityError):
            await store.get_bytes(org_a, art.id)

    run(provisioned_db, s3, scenario)


def test_size_limit_and_unknown_kind(provisioned_db, s3, orgs):
    org_a, _, _ = orgs

    async def scenario(store):
        with pytest.raises(ArtifactTooLargeError):
            await store.put(org_a, b"x" * 11, kind="other", content_type="text/plain")
        with pytest.raises(ValueError, match="unknown artifact kind"):
            await store.put(org_a, b"x", kind="malware", content_type="text/plain")

    run(provisioned_db, s3, scenario, max_bytes=10)


def test_presigned_urls_are_short_lived(provisioned_db, s3, orgs):
    org_a, _, wf = orgs

    async def scenario(store):
        art = await store.put(org_a, b"log", kind="execution_log", content_type="text/plain")
        return await store.presign_download(org_a, art.id, ttl_seconds=3600), store.presign_upload(org_a, wf)

    url, upload = run(provisioned_db, s3, scenario, presign_ttl_seconds=300)
    assert "X-Amz-Expires=300" in url  # 3600 requested, clamped to the configured maximum
    policy = json.loads(base64.b64decode(upload.fields["policy"]))
    assert ["content-length-range", 1, upload.max_bytes] in policy["conditions"]
    assert {"key": upload.key} in policy["conditions"]
    assert upload.key.startswith(f"orgs/{org_a}/workflows/{wf}/uploads/") and upload.expires_in == 300


def test_finalize_upload_moves_to_content_address(provisioned_db, s3, orgs):
    org_a, org_b, wf = orgs

    async def scenario(store):
        upload = store.presign_upload(org_a, wf)
        s3.put_object(Bucket=BUCKET, Key=upload.key, Body=b"patch from sandbox")  # what the sandbox POST does
        art = await store.finalize_upload(org_a, wf, upload.key, kind="patch", content_type="text/x-diff")
        assert await store.get_bytes(org_a, art.id) == b"patch from sandbox"
        with pytest.raises(NotFoundError):  # upload object was removed
            await store.finalize_upload(org_a, wf, upload.key, kind="patch", content_type="text/x-diff")
        # A key from another tenant's (or workflow's) prefix is never accepted.
        other = store.presign_upload(org_b, uuid.uuid4())
        with pytest.raises(ConflictError):
            await store.finalize_upload(org_a, wf, other.key, kind="patch", content_type="text/x-diff")
        with pytest.raises(ConflictError):
            await store.finalize_upload(org_a, wf, upload.key + "/../../x", kind="patch", content_type="text/x-diff")

    run(provisioned_db, s3, scenario)

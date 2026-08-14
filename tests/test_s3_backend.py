"""
S3 backend tests, using moto to simulate the S3 API.

The S3 backend had no test coverage at all — every test in this suite ran
against LocalFSBackend, so the code path that actually runs in production
was validated only by inspection. These cover the parts where S3 semantics
differ from a filesystem:

  - conditional writes (If-None-Match / If-Match), which the entire
    optimistic-concurrency scheme depends on
  - key prefixing, which only S3Backend does
  - the 403-vs-404 behavior for absent keys, which is the single most
    common way an otherwise-correct S3 deployment fails

Skipped automatically if moto isn't installed:  pip install "moto[s3]"
"""

from __future__ import annotations

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from botocore.exceptions import ClientError  # noqa: E402

from app.graph.store import EntityGraphStore  # noqa: E402
from app.storage.backend import ConflictError, S3Backend  # noqa: E402

BUCKET = "memory-backend-test"


@pytest.fixture()
def s3_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield


@pytest.fixture()
def backend(s3_env):
    return S3Backend(bucket=BUCKET, prefix="graph")


# ---------- basic semantics ----------

def test_put_get_roundtrip(backend):
    etag = backend.put_bytes("wikis/u/person/a.okf.md", b"hello")
    got = backend.get_bytes("wikis/u/person/a.okf.md")
    assert got.data == b"hello"
    assert got.etag == etag


def test_missing_key_returns_none(backend):
    assert backend.get_bytes("wikis/u/person/nobody.okf.md") is None


def test_prefix_is_applied_and_stripped(backend):
    backend.put_bytes("wikis/u/person/a.okf.md", b"x")
    raw = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket=BUCKET)
    stored = [o["Key"] for o in raw["Contents"]]

    assert stored == ["graph/wikis/u/person/a.okf.md"], "prefix must be applied on write"
    assert backend.list_keys("wikis/u/") == ["wikis/u/person/a.okf.md"], "and stripped on read"


def test_delete(backend):
    backend.put_bytes("wikis/u/person/a.okf.md", b"x")
    backend.delete("wikis/u/person/a.okf.md")
    assert backend.get_bytes("wikis/u/person/a.okf.md") is None


# ---------- conditional writes: the whole concurrency scheme ----------

def test_if_none_match_rejects_overwrite(backend):
    """if_match="" means 'must not already exist' -> IfNoneMatch: *"""
    backend.put_bytes("wikis/u/person/a.okf.md", b"first")
    with pytest.raises(ConflictError):
        backend.put_bytes("wikis/u/person/a.okf.md", b"second", if_match="")


def test_if_none_match_allows_create(backend):
    backend.put_bytes("wikis/u/person/new.okf.md", b"first", if_match="")
    assert backend.get_bytes("wikis/u/person/new.okf.md").data == b"first"


def test_if_match_rejects_stale_etag(backend):
    backend.put_bytes("wikis/u/person/a.okf.md", b"v1")
    with pytest.raises(ConflictError):
        backend.put_bytes("wikis/u/person/a.okf.md", b"v2", if_match="0" * 32)


def test_if_match_accepts_current_etag(backend):
    etag = backend.put_bytes("wikis/u/person/a.okf.md", b"v1")
    backend.put_bytes("wikis/u/person/a.okf.md", b"v2", if_match=etag)
    assert backend.get_bytes("wikis/u/person/a.okf.md").data == b"v2"


# ---------- the deployment failure mode ----------

def test_access_denied_on_get_explains_listbucket(s3_env):
    """Without s3:ListBucket, S3 answers 403 (not 404) for absent keys, so
    every existence check raises. The error must name the actual cause."""

    class DeniedClient:
        def get_object(self, **kw):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "GetObject"
            )

    backend = S3Backend(bucket=BUCKET, client=DeniedClient())
    with pytest.raises(PermissionError, match="s3:ListBucket"):
        backend.get_bytes("wikis/u/person/nobody.okf.md")


def test_unsupported_conditional_write_is_explained(s3_env):
    """S3-compatible stores that don't implement conditional writes must fail
    with an explanation, not a bare NotImplemented."""

    class NoCondClient:
        def put_object(self, **kw):
            raise ClientError(
                {"Error": {"Code": "NotImplemented", "Message": "not supported"}}, "PutObject"
            )

    backend = S3Backend(bucket=BUCKET, client=NoCondClient())
    with pytest.raises(RuntimeError, match="conditional write"):
        backend.put_bytes("wikis/u/person/a.okf.md", b"x", if_match="abc")


# ---------- the store on top of S3 ----------

def test_full_store_lifecycle_on_s3(backend):
    store = EntityGraphStore(backend)

    store.upsert_entity("u1", "person", "Alice Chen", summary_append="Engineer.")
    store.upsert_entity("u1", "project", "Orion", summary_append="Search project.")
    store.link_entities("u1", "person/alice-chen", "project/orion",
                        label="works_on", bidirectional=True)
    store.add_fact("u1", "person/alice-chen", "Joined in 2024.")
    store.flush()

    entity = store.get_entity("u1", "person/alice-chen", touch=False)
    assert entity.title == "Alice Chen"
    assert len(entity.facts) == 1
    assert entity.relations[0].target == "project/orion"

    assert set(store.traverse("u1", ["person/alice-chen"], max_depth=2)) == {
        "person/alice-chen", "project/orion"
    }

    store.delete_entity("u1", "project/orion", cascade=True)
    store.flush()
    assert store.get_entity("u1", "project/orion", touch=False) is None
    assert store.get_entity("u1", "person/alice-chen", touch=False).relations == []


def test_manifest_rebuild_on_s3(backend):
    """The buffered-manifest recovery path has to work against real object
    storage, not just a local filesystem."""
    store = EntityGraphStore(backend)
    for i in range(5):
        store.upsert_entity("u1", "concept", f"C{i}")
    store.flush()

    for key in list(backend.list_keys("wikis/u1/_manifest")):
        backend.delete(key)
    backend.delete("wikis/u1/_manifest.json")
    store.manifest._cache.clear()
    assert store.list_entities("u1") == []

    assert store.manifest.rebuild("u1") == 5
    assert len(store.list_entities("u1")) == 5


def test_from_s3_config_actually_constructs(backend):
    """Every other test in this suite reaches MirageBackend through
    from_disk(). Nothing exercised from_s3_config(), so a NameError in it —
    a parameter used in the body but missing from the signature — passed a
    fully green suite and only surfaced against a real bucket.

    Constructing it is enough to catch that class of bug.
    """
    from app.storage.mirage_backend import MirageBackend

    created = MirageBackend.from_s3_config(
        bucket="test-bucket",
        region="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        reuse_connections=False,
    )
    try:
        assert created is not None
    finally:
        created.close()


def test_both_constructors_accept_the_same_tuning_options():
    """from_disk and from_s3_config must stay in step. They drifted once:
    verify_conditional_writes was added to one and used in the body of the
    other, which only fails at runtime and only on the S3 path."""
    import inspect
    from app.storage.mirage_backend import MirageBackend

    disk = set(inspect.signature(MirageBackend.from_disk).parameters)
    s3 = set(inspect.signature(MirageBackend.from_s3_config).parameters)

    shared = {"index_ttl", "file_cache_limit", "reuse_connections",
              "verify_conditional_writes"}
    missing_disk = shared - disk
    missing_s3 = shared - s3
    assert not missing_disk, f"from_disk missing {missing_disk}"
    assert not missing_s3, f"from_s3_config missing {missing_s3}"

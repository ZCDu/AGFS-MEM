"""
Standalone mirage/S3 connectivity diagnostic. Run this directly:

    python scripts/diagnose_mirage.py

It does NOT go through FastAPI, uvicorn, --reload, or any of the app's
cached settings (deps.py's @lru_cache, config.py's module-level cache).
It loads .env itself, prints exactly what it's about to use (so there's
no ambiguity about stale values), and attempts one write + one read
directly against mirage/S3. Whatever error comes out of this is the
real, unfiltered error — no generic "Internal server error" in the way.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    loaded = load_dotenv()
    print(f".env loaded: {loaded}")
except ImportError:
    print(".env loaded: False (python-dotenv not installed)")

print()
print("=== Values this script is about to use ===")
bucket = os.environ.get("MIRAGE_S3_BUCKET")
region = os.environ.get("MIRAGE_S3_REGION")
endpoint = os.environ.get("MIRAGE_S3_ENDPOINT_URL")
access_key = os.environ.get("MIRAGE_S3_ACCESS_KEY_ID")
secret_key = os.environ.get("MIRAGE_S3_SECRET_ACCESS_KEY")
path_style = os.environ.get("MIRAGE_S3_PATH_STYLE", "false")
key_prefix = os.environ.get("MIRAGE_S3_KEY_PREFIX", "memory_backend/")

print(f"MIRAGE_S3_BUCKET       = {bucket!r}")
print(f"MIRAGE_S3_REGION       = {region!r}")
print(f"MIRAGE_S3_ENDPOINT_URL = {endpoint!r}")
print(f"MIRAGE_S3_ACCESS_KEY_ID     = {access_key[:6] + '...' if access_key else None!r}")
print(f"MIRAGE_S3_SECRET_ACCESS_KEY = {'set (' + str(len(secret_key)) + ' chars)' if secret_key else None}")
print(f"MIRAGE_S3_PATH_STYLE   = {path_style!r}")
print(f"MIRAGE_S3_KEY_PREFIX   = {key_prefix!r}")
print()

if not bucket:
    print("STOP: MIRAGE_S3_BUCKET is not set at all. Fix .env before continuing.")
    sys.exit(1)
if not access_key or not secret_key:
    print("STOP: access key or secret key is missing. Fix .env before continuing.")
    sys.exit(1)

print("=== Attempting connection ===")
try:
    from app.storage.mirage_backend import MirageBackend
except ImportError as e:
    print(f"STOP: couldn't import MirageBackend: {e}")
    print("Are you running this from the memory_backend directory? "
          "(python scripts/diagnose_mirage.py, from the project root)")
    sys.exit(1)

try:
    backend = MirageBackend.from_s3_config(
        bucket=bucket,
        region=region,
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        path_style=path_style.lower() == "true",
        key_prefix=key_prefix,
    )
    print("Backend constructed OK.")
except Exception:
    print("FAILED constructing the backend (before any network call):")
    import traceback
    traceback.print_exc()
    sys.exit(1)

test_key = "diagnose_mirage_test.txt"
try:
    print(f"\nWriting test object: {test_key!r} ...")
    etag = backend.put_bytes(test_key, b"hello from diagnose_mirage.py")
    print(f"WRITE OK. etag={etag!r}")

    print(f"\nReading it back...")
    result = backend.get_bytes(test_key)
    print(f"READ OK. data={result.data!r}")

    print(f"\nCleaning up...")
    backend.delete(test_key)
    print("DELETE OK.")

    print("\n=== Now testing the FULL upsert_entity flow (what PUT /wiki actually does) ===")
    print("This is 3 sequential writes: entity file, manifest, ops log —")
    print("timestamped individually so we can see exactly which one is slow.")
    from app.graph.store import EntityGraphStore
    import time

    store = EntityGraphStore(backend)

    t0 = time.perf_counter()
    entity = store.upsert_entity("u_diagnose", "person", "DiagnoseTestPerson")
    t1 = time.perf_counter()
    print(f"upsert_entity() total: {t1 - t0:.2f}s  ->  wiki_id={entity.wiki_id!r}")

    print("\nCleaning up the test entity...")
    store.delete_entity("u_diagnose", entity.wiki_id, cascade=False, hard_delete=True)

    print("\n=== SUCCESS: mirage/S3 connectivity AND the full write flow work correctly. ===")
    print("If the FastAPI app is still failing, the problem is in how the")
    print("running server process picked up its config (stale process, wrong")
    print(".env location, or it needs a real restart) — not in the storage code.")

except Exception:
    print("\n=== FAILED. Full error below: ===")
    import traceback
    traceback.print_exc()
    print()
    print("This is the real error, unfiltered by FastAPI's generic 500 handler.")
    print("Paste everything above (including 'Values this script is about to")
    print("use') — that tells us definitively what's wrong.")
finally:
    try:
        backend.close()
    except Exception:
        pass

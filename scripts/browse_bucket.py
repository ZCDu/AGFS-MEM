"""
List and download objects from the actual configured storage backend
(Qiniu Kodo via mirage, in production; local disk in dev).

Unlike check_bucket.py (which reads a local disk bucket directly), this goes
through the same StorageBackend the running app uses -- same credentials,
same endpoint, same TLS setup -- so it works for the real Qiniu bucket
without any new tools, accounts, or config.

Run:
    python scripts/browse_bucket.py list [prefix]
    python scripts/browse_bucket.py get <key> [local_path]

Examples:
    python scripts/browse_bucket.py list
    python scripts/browse_bucket.py list miguel/wiki/
    python scripts/browse_bucket.py get miguel/wiki/_manifest/snapshot.json snapshot.json
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv()
except ImportError:
    pass

from app.deps import get_storage_backend  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "get"):
        print(__doc__)
        sys.exit(1)

    backend = get_storage_backend()
    cmd = sys.argv[1]

    if cmd == "list":
        prefix = sys.argv[2] if len(sys.argv) > 2 else ""
        keys = sorted(backend.list_keys(prefix))
        if not keys:
            print(f"No keys found under prefix {prefix!r}.")
        for k in keys:
            print(k)
        print(f"\n{len(keys)} key(s)")

    elif cmd == "get":
        if len(sys.argv) < 3:
            print("Usage: python scripts/browse_bucket.py get <key> [local_path]")
            sys.exit(1)
        key = sys.argv[2]
        local_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(key.split("/")[-1])
        result = backend.get_bytes(key)
        if result is None:
            print(f"Not found: {key!r}")
            sys.exit(1)
        local_path.write_bytes(result.data)
        print(f"Saved {len(result.data)} bytes -> {local_path.resolve()}")

    close = getattr(backend, "close", None)
    if callable(close):
        close()


if __name__ == "__main__":
    main()

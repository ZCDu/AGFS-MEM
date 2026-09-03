"""
Scan a local_bucket for entity files that cannot be parsed.

Run:  python scripts/check_bucket.py [--root ./local_bucket] [--fix]

Why this exists: a corrupt .okf.md file used to surface as an opaque HTTP
500 on any route that touched it, including deletes — which made it look
like the delete endpoints were broken rather than one file being bad. The
API now returns 422 naming the entity, but if several are broken you want
the whole list at once, offline, without guessing which request to send.

--fix moves broken files aside to <name>.corrupt so the rest of the graph
becomes usable again. It never deletes anything. After using it, call
POST /v1/users/{user_id}/wiki/_rebuild_manifest so the index stops listing
entities whose files are gone.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph.store import EntityGraphStore, MalformedEntityError  # noqa: E402


def resolve_default_root() -> str:
    """Resolve the bucket root the way the app does.

    app/main.py calls load_dotenv() at import time, so the running server
    picks up LOCAL_BUCKET_ROOT from a .env file. This script imports only
    app.graph.store, which does not, so without this it would silently
    check ./local_bucket while the server wrote somewhere else entirely —
    exactly the confusion this script is meant to resolve.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
        # find_dotenv() defaults to searching upward from the CALLING FILE's
        # directory, not the working directory. For a script under scripts/
        # that means it looks in scripts/ and above, and misses a .env sitting
        # next to where the user actually is. Search the cwd first, then fall
        # back to the file-relative search that app/main.py performs, so this
        # agrees with the running server either way. load_dotenv does not
        # override variables already set, so first match wins.
        load_dotenv(find_dotenv(usecwd=True))
        load_dotenv()
    except ImportError:
        pass
    return os.environ.get("LOCAL_BUCKET_ROOT", "./local_bucket")


def find_buckets(max_depth: int = 4) -> list[Path]:
    """Look for directories containing entity files, so a wrong --root gives
    a useful pointer instead of a dead end."""
    seen: dict[Path, int] = {}
    for base in (Path.cwd(), Path.cwd().parent, Path.home()):
        if not base.is_dir():
            continue
        try:
            for f in base.rglob("*.okf.md"):
                try:
                    depth = len(f.relative_to(base).parts)
                except ValueError:
                    continue
                if depth > max_depth + 4:
                    continue
                # Entity files live at <root>/<user>/wiki/<type>/<slug>.okf.md,
                # so the bucket root is four levels up from the file — type,
                # wiki, user, root. Three lands on the user directory and
                # sends the operator to a --root that will not work.
                bucket = f.parent.parent.parent.parent
                seen[bucket] = seen.get(bucket, 0) + 1
        except (PermissionError, OSError):
            continue
    return sorted(seen, key=lambda p: -seen[p])[:5]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="bucket root. Default: resolved the same way the app resolves it "
                         "(LOCAL_BUCKET_ROOT from the environment or a .env file, "
                         "else ./local_bucket)")
    ap.add_argument("--fix", action="store_true",
                    help="rename unreadable files to <name>.corrupt (never deletes)")
    args = ap.parse_args()

    root = Path(args.root) if args.root else Path(resolve_default_root())
    print(f"  looking in: {root.resolve()}")

    if not root.is_dir():
        print(f"\n  No such directory: {root.resolve()}\n")
        candidates = find_buckets()
        if candidates:
            print("  Found entity files elsewhere — the server was probably started")
            print("  from a different working directory. Try one of these:\n")
            for c in candidates:
                n = len(list(c.rglob("*.okf.md")))
                print(f"    --root \"{c}\"   ({n} entity file(s))")
        else:
            print("  No *.okf.md files found nearby either. Either nothing has been")
            print("  written yet, or STORAGE_BACKEND is not 'local'. Check with:")
            print("    Invoke-RestMethod -Uri http://localhost:8000/healthz")
        sys.exit(1)

    files = sorted(root.rglob("*.okf.md"))
    if not files:
        print(f"No entity files found under {root}")
        return

    ok = 0
    broken: list[tuple[Path, str]] = []
    for path in files:
        try:
            EntityGraphStore._deserialize(path.read_bytes())
            ok += 1
        except MalformedEntityError as e:
            broken.append((path, str(e)))
        except Exception as e:  # anything else is still a broken file
            broken.append((path, f"{type(e).__name__}: {e}"))

    print(f"\n  scanned {len(files)} entity file(s) under {root}")
    print(f"  readable: {ok}")
    print(f"  corrupt:  {len(broken)}\n")

    for path, err in broken:
        rel = path.relative_to(root)
        print(f"  BROKEN  {rel}")
        print(f"          {err}")
        if args.fix:
            target = path.with_suffix(path.suffix + ".corrupt")
            os.replace(path, target)
            print(f"          moved aside -> {target.name}")

    # Stray .tmp files mean a write was interrupted between open() and the
    # atomic rename. Harmless on their own, but a useful signal that the
    # process was killed mid-write.
    tmps = sorted(root.rglob("*.tmp"))
    if tmps:
        print(f"\n  {len(tmps)} stray .tmp file(s) — a write was interrupted:")
        for t in tmps:
            print(f"    {t.relative_to(root)}")

    if broken and args.fix:
        print("\n  Now run POST /v1/users/{user_id}/wiki/_rebuild_manifest to drop")
        print("  index entries for the files you moved aside.")
    elif broken:
        print("\n  Re-run with --fix to move these aside.")


if __name__ == "__main__":
    main()

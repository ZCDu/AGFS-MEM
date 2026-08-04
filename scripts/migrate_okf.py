"""
Rewrite existing entity files into the OKF v0.2 spec format.

    python scripts/migrate_okf.py --user demo --dry-run
    python scripts/migrate_okf.py --user demo

Rewrites entity files to the current format: .okf.md → .md, companion JSON
removal, status values updated. It is idempotent: running it twice changes
nothing the second time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv()
except ImportError:
    pass

from app.config import get_settings  # noqa: E402
from app.deps import get_graph_store, get_storage_backend  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    store = get_graph_store()
    backend = get_storage_backend()

    print(f"  storage         : {settings.storage_backend}")

    wiki_ids = store.list_entities(args.user)
    print(f"  entities        : {len(wiki_ids)}")

    changed = 0
    for wiki_id in wiki_ids:
        # Check for old .okf.md files that need migration
        old_md_key = f"{args.user}/wiki/{wiki_id}.okf.md"
        old_json_key = f"{args.user}/wiki/{wiki_id}.okf.json"
        new_md_key = f"{args.user}/wiki/{wiki_id}.md"

        raw = backend.get_bytes(old_md_key)
        if raw is None:
            # Already migrated or doesn't exist
            continue

        changed += 1
        if args.dry_run:
            print(f"    would migrate {wiki_id}")
            continue

        entity = store.get_entity(args.user, wiki_id, touch=False)
        if entity is None:
            continue
        # Re-serialise through the normal write path so derived state follows.
        store.upsert_entity(args.user, entity.type, entity.title)
        # Clean up old files
        try:
            backend.delete(old_md_key)
        except Exception:
            pass
        try:
            backend.delete(old_json_key)
        except Exception:
            pass
        print(f"    migrated {wiki_id}")

    store.flush()
    verb = "would be migrated" if args.dry_run else "migrated"
    print(f"\n  {changed} file(s) {verb}; {len(wiki_ids) - changed} already current.")

    close = getattr(backend, "close", None)
    if callable(close):
        close()


if __name__ == "__main__":
    main()

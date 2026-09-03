"""
Rewrite existing entity files into the configured OKF_MODE.

    python scripts/migrate_okf.py --user demo --dry-run
    python scripts/migrate_okf.py --user demo

Reads work under either mode regardless of which wrote the file, so switching
OKF_MODE needs no migration to keep working — but files already on disk keep
their old shape until something writes them again. This rewrites them now, so
a bundle is consistent rather than half in one layout and half in the other.

Rewriting is a read-then-write of each entity through the normal path, so the
manifest, adjacency index and ops log all stay correct. It is idempotent:
running it twice changes nothing the second time.
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
from app.graph.store import OKF_MODE_COMPANION  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    store = get_graph_store()
    backend = get_storage_backend()
    mode = settings.okf_mode

    print(f"  target OKF_MODE : {mode}")
    print(f"  storage         : {settings.storage_backend}")

    wiki_ids = store.list_entities(args.user)
    print(f"  entities        : {len(wiki_ids)}")

    changed = 0
    for wiki_id in wiki_ids:
        md_key = f"{args.user}/wiki/{wiki_id}.okf.md"
        json_key = f"{args.user}/wiki/{wiki_id}.okf.json"
        raw = backend.get_bytes(md_key)
        if raw is None:
            continue
        text = raw.data.decode("utf-8", "replace")
        has_companion = backend.get_bytes(json_key) is not None
        # "facts:" in the frontmatter means the structured data is inline.
        inline = "\nfacts:" in text.split("---", 2)[1] if text.startswith("---") else False

        wants_companion = mode == OKF_MODE_COMPANION
        if wants_companion == has_companion and wants_companion != inline:
            continue

        changed += 1
        if args.dry_run:
            print(f"    would rewrite {wiki_id}")
            continue

        entity = store.get_entity(args.user, wiki_id, touch=False)
        if entity is None:
            continue
        # Re-serialise through the normal write path so derived state follows.
        store.upsert_entity(args.user, entity.type, entity.title)
        print(f"    rewrote {wiki_id}")

    store.flush()
    verb = "would be rewritten" if args.dry_run else "rewritten"
    print(f"\n  {changed} file(s) {verb}; {len(wiki_ids) - changed} already correct.")

    close = getattr(backend, "close", None)
    if callable(close):
        close()


if __name__ == "__main__":
    main()

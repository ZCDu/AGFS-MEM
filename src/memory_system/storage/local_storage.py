import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp",
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma",
    ".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv",
}


class LocalStorage:
    def __init__(self, base_path: str):
        self._base = Path(base_path).expanduser().resolve()

    def _user_dir(self, user_id: str) -> Path:
        user_dir = (self._base / user_id).resolve()
        if not user_dir.is_relative_to(self._base):
            raise ValueError("user_id resolves outside storage base path")
        return user_dir

    def _ensure_dirs(self, user_id: str):
        root = self._user_dir(user_id)
        for sub in ("journals", "raw", "sources", "assets"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _is_media(path_or_url: str) -> bool:
        """Check if the file extension indicates image/audio/video."""
        # Strip query string for URL-based judgments
        parsed = urlparse(path_or_url)
        path = parsed.path if parsed.scheme else path_or_url
        suffix = Path(path).suffix.lower()
        return suffix in MEDIA_EXTENSIONS

    @staticmethod
    def _classify(path_or_url: str) -> str:
        return "assets" if LocalStorage._is_media(path_or_url) else "raw"

    @staticmethod
    def _extract_files(messages: list[dict]) -> list[dict]:
        """Extract file attachments from messages list.

        Returns list of {"type":"file", "file_url":"...", "message_idx": N}
        """
        files = []
        for idx, msg in enumerate(messages):
            content = msg.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "file" and item.get("file_url"):
                        files.append({
                            "type": "file",
                            "file_url": item["file_url"],
                            "message_idx": idx,
                        })
        return files

    async def save_file(self, user_id: str, file_url: str) -> str | None:
        """Download (remote URL) or copy (local path) a file into storage.

        Returns the relative path within the user directory, e.g. 'raw/abc-report.pdf'.
        """
        user_root = self._ensure_dirs(user_id)
        category = self._classify(file_url)
        parsed = urlparse(file_url)

        # Derive the local source path and basename
        if parsed.scheme in ("http", "https"):
            remote_path = parsed.path or "/"
            filename = Path(remote_path).name or "download"
            src_path = None
        elif parsed.scheme == "file":
            src_path = Path(parsed.path)
            filename = src_path.name or "file"
        else:
            src_path = Path(file_url)
            filename = src_path.name or "file"

        # Prefix with short hash to avoid collisions
        h = hashlib.md5(file_url.encode()).hexdigest()[:8]
        dest_name = f"{h}-{filename}"
        dest_path = user_root / category / dest_name

        try:
            if parsed.scheme in ("http", "https"):
                async with httpx.AsyncClient() as client:
                    resp = await client.get(file_url, timeout=60.0, follow_redirects=True)
                    resp.raise_for_status()
                    dest_path.write_bytes(resp.content)
            elif src_path and src_path.exists():
                shutil.copy2(src_path, dest_path)
            else:
                logger.warning(f"Local file not found: {file_url}")
                return None

            logger.info(f"Saved file: {file_url} → {dest_path}")
            return f"{category}/{dest_name}"
        except Exception as e:
            logger.error(f"Failed to save file {file_url}: {e}")
            return None

    async def write_journal(
        self, user_id: str, entries: list[dict],
    ):
        """Append entries to today's journal file (JSONL).

        Each entry is a dict that will be serialized as one JSON line.
        """
        self._ensure_dirs(user_id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        journal_path = self._user_dir(user_id) / "journals" / f"{today}.jsonl"

        try:
            with open(journal_path, "a", encoding="utf-8") as f:
                for entry in entries:
                    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info(f"Journal written: {journal_path} ({len(entries)} entries)")
        except Exception as e:
            logger.error(f"Failed to write journal: {e}")

"""
Raw uploaded files.

    {user_id}/raw/{session_id}/{file_id}/content        the bytes, unmodified
    {user_id}/raw/{session_id}/{file_id}/meta.json      name, type, size, hash

Files are organized by session, not by date. A session groups all uploads
from one conversation, making it natural to browse, list, and clean up.

WHY THE BYTES ARE KEPT UNCHANGED
    This is the archival copy. Text is extracted for the model separately and
    the extraction is lossy — a PDF becomes a flat string, a CSV loses its
    shape. If only the extracted text were kept, improving extraction later
    could never be applied to files already uploaded, and there would be no
    way to check what the original actually said.

    That is the same reason conversations are journalled verbatim rather than
    only as the facts pulled out of them: the derived form is a convenience,
    the original is the record.

WHY METADATA IS A SIBLING OBJECT
    Listing a session's uploads should not mean downloading every file.
    meta.json is small and can be read alone; `content` is only fetched when
    the bytes are actually wanted.

WHAT IS AND IS NOT SUPPORTED
    Text formats are extracted for the model. Binary formats (PDF, DOCX,
    images) are stored faithfully but report that their text could not be
    read, rather than silently handing the model mojibake — a model given
    garbled bytes will confidently invent content, which is worse than being
    told the file is unreadable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.storage.backend import StorageBackend

logger = logging.getLogger("memory_backend.files")

_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Attachments are injected into prompts, so an unbounded file becomes an
# unbounded bill and an over-length request.
MAX_TEXT_CHARS = 60_000

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp",
    ".go", ".rs", ".rb", ".php", ".sh", ".sql", ".env", ".srt", ".vtt",
}

BINARY_HINTS = {
    ".pdf": "PDF", ".docx": "Word document", ".doc": "Word document",
    ".xlsx": "Excel workbook", ".xls": "Excel workbook", ".pptx": "PowerPoint",
    ".zip": "archive", ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".webp": "image", ".mp3": "audio", ".mp4": "video",
}


class FileError(ValueError):
    pass


def new_file_id(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"{when.strftime('%H%M%S')}-{secrets.token_hex(3)}"


def validate_file_id(file_id: str) -> str:
    if not _FILE_ID.match(file_id or ""):
        raise FileError(
            f"Invalid file id {file_id!r}: letters, digits, dot, dash or "
            f"underscore, 1-64 characters.")
    return file_id


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID.match(session_id or ""):
        raise FileError(
            f"Invalid session id {session_id!r}: letters, digits, dot, dash or "
            f"underscore, 1-128 characters.")
    return session_id


def safe_name(name: str) -> str:
    """Keep the original name for display only — never for the storage key.

    The key uses a generated id, so a hostile or merely awkward filename
    cannot escape its prefix, collide with another upload, or overwrite
    anything.
    """
    cleaned = re.sub(r"[\r\n\t]", " ", (name or "").strip())[:200]
    return cleaned or "unnamed"


def suffix_of(name: str) -> str:
    idx = name.rfind(".")
    return name[idx:].lower() if idx > 0 else ""


def extract_text(data: bytes, name: str) -> tuple[str | None, str]:
    """Returns (text, note). text is None when the file cannot be read.

    Decoding is attempted for known-text suffixes and for anything that looks
    like text; binary formats are refused explicitly. Handing a model decoded
    binary produces confident invention, which is worse than telling it the
    file is unreadable.
    """
    suffix = suffix_of(name)

    if suffix in BINARY_HINTS:
        return None, (f"{BINARY_HINTS[suffix]} files are stored but their text "
                      f"cannot be read yet; the model was not given contents.")

    if b"\x00" in data[:4096]:
        return None, "Looks binary (contains null bytes); contents not given to the model."

    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None, "Could not decode as text; contents not given to the model."

    if suffix not in TEXT_SUFFIXES:
        # Decoded, but the suffix is unknown. Accept it — refusing would block
        # perfectly readable files with unusual extensions — and say so.
        note = f"Unrecognised extension {suffix or '(none)'}; treated as plain text."
    else:
        note = ""

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        note = (note + " " if note else "") + (
            f"Truncated to {MAX_TEXT_CHARS} characters for the model; the "
            f"stored copy is complete.")
    return text, note


@dataclass
class FileMeta:
    file_id: str
    name: str
    session_id: str
    size: int
    sha256: str
    content_type: str = ""
    text_extractable: bool = False
    note: str = ""
    uploaded_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "FileMeta":
        defaults = asdict(FileMeta("", "", "", 0, ""))
        return FileMeta(**{k: d.get(k, v) for k, v in defaults.items()})


class FileStore:
    def __init__(self, backend: StorageBackend):
        self.backend = backend

    def _dir(self, user_id: str, session_id: str, file_id: str) -> str:
        return f"{user_id}/raw/{session_id}/{file_id}"

    def _session_prefix(self, user_id: str, session_id: str) -> str:
        return f"{user_id}/raw/{session_id}/"

    def save(self, user_id: str, name: str, data: bytes,
             content_type: str = "", session_id: str = "",
             when: datetime | None = None) -> FileMeta:
        when = when or datetime.now(timezone.utc)
        file_id = new_file_id(when)
        text, note = extract_text(data, name)

        sid = validate_session_id(session_id) if session_id else "_unsorted"

        meta = FileMeta(
            file_id=file_id, name=safe_name(name), session_id=sid,
            size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type or "", text_extractable=text is not None,
            note=note, uploaded_at=when.isoformat(),
        )

        base = self._dir(user_id, sid, file_id)
        # Bytes first: metadata pointing at content that does not exist is
        # worse than content with no metadata.
        self.backend.put_bytes(f"{base}/content", data)
        self.backend.put_bytes(
            f"{base}/meta.json",
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
        logger.info("stored raw file %s (%s, %d bytes) for %s in session %s",
                    file_id, meta.name, meta.size, user_id, sid)
        return meta

    def get_meta(self, user_id: str, file_id: str, session_id: str) -> FileMeta | None:
        validate_file_id(file_id)
        validate_session_id(session_id)
        raw = self.backend.get_bytes(f"{self._dir(user_id, session_id, file_id)}/meta.json")
        if raw is None:
            return None
        return FileMeta.from_dict(json.loads(raw.data.decode("utf-8")))

    def get_bytes(self, user_id: str, file_id: str, session_id: str) -> bytes | None:
        validate_file_id(file_id)
        validate_session_id(session_id)
        raw = self.backend.get_bytes(f"{self._dir(user_id, session_id, file_id)}/content")
        return raw.data if raw else None

    def get_text(self, user_id: str, file_id: str,
                 session_id: str) -> tuple[str | None, FileMeta | None]:
        """The extracted text, re-derived from the stored bytes.

        Deliberately not cached: extraction improves over time, and re-reading
        the original means old uploads benefit from that. The archival bytes
        are what make this possible.
        """
        meta = self.get_meta(user_id, file_id, session_id)
        if meta is None:
            return None, None
        data = self.get_bytes(user_id, file_id, session_id)
        if data is None:
            return None, meta
        text, _ = extract_text(data, meta.name)
        return text, meta

    def list_session(self, user_id: str, session_id: str) -> list[FileMeta]:
        validate_session_id(session_id)
        out = []
        for key in self.backend.list_keys(self._session_prefix(user_id, session_id)):
            if not key.endswith("/meta.json"):
                continue
            raw = self.backend.get_bytes(key)
            if raw is None:
                continue
            try:
                out.append(FileMeta.from_dict(json.loads(raw.data.decode("utf-8"))))
            except (json.JSONDecodeError, TypeError):
                logger.warning("unreadable file metadata at %s", key)
        return sorted(out, key=lambda m: m.uploaded_at)

    def list_sessions(self, user_id: str) -> list[str]:
        """List session IDs that have uploaded files."""
        prefix = f"{user_id}/raw/"
        seen: set[str] = set()
        for key in self.backend.list_keys(prefix):
            # key: {user_id}/raw/{session_id}/{file_id}/...
            parts = key.removeprefix(prefix).split("/")
            if len(parts) >= 2 and parts[0] not in ("", "_unsorted"):
                seen.add(parts[0])
        return sorted(seen)

    def find(self, user_id: str, file_id: str,
             session_ids: list[str] | None = None,
             search_sessions: int = 50) -> str | None:
        """Locate a file's session. Callers that know it should pass it."""
        validate_file_id(file_id)
        if session_ids is None:
            session_ids = self.list_sessions(user_id)
        for sid in session_ids[:search_sessions]:
            if self.backend.get_bytes(f"{self._dir(user_id, sid, file_id)}/meta.json"):
                return sid
        # also check _unsorted
        if self.backend.get_bytes(f"{self._dir(user_id, '_unsorted', file_id)}/meta.json"):
            return "_unsorted"
        return None


def file_ref(session_id: str, file_id: str) -> str:
    """The form recorded in a fact's `evidence` list, alongside session refs."""
    return f"file:{session_id}:{file_id}"


def parse_file_ref(ref: str) -> tuple[str, str] | None:
    if not ref.startswith("file:"):
        return None
    parts = ref.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        return validate_session_id(parts[1]), validate_file_id(parts[2])
    except (ValueError, FileError):
        return None

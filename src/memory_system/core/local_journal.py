import logging

logger = logging.getLogger(__name__)


class LocalJournalPipeline:
    """Persist attachments and journal entries through a replaceable storage backend."""

    def __init__(self, storage):
        self._storage = storage

    async def save_messages(self, user_id: str, messages: list[dict]) -> None:
        file_attachments = self._storage._extract_files(messages)

        file_map: dict[str, str] = {}
        for attachment in file_attachments:
            local_path = await self._storage.save_file(user_id, attachment["file_url"])
            if local_path:
                file_map[attachment["file_url"]] = local_path

        entries = self._build_entries(messages, file_map)
        if entries:
            await self._storage.write_journal(user_id, entries)

    @staticmethod
    def _build_entries(messages: list[dict], file_map: dict[str, str]) -> list[dict]:
        entries = []
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if item.get("type") == "input_text" and item.get("text"):
                        text_parts.append(item["text"])
                    elif item.get("type") == "file" and item.get("file_url"):
                        url = item["file_url"]
                        entries.append(
                            {
                                "type": "file",
                                "original_url": url,
                                "local_path": file_map.get(url, url),
                            }
                        )
                content = " ".join(text_parts)

            if isinstance(content, str) and content.strip():
                entries.append(
                    {
                        "type": "message",
                        "role": message.get("role", "unknown"),
                        "content": content,
                    }
                )
        return entries

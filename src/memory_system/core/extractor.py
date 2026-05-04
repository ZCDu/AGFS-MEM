import json
from memory_system.config import Settings
from memory_system.utils.text_utils import messages_to_text


EXTRACTION_PROMPT = """你是一个记忆提取系统。分析以下对话，提取值得长期记住的用户信息。

只提取具有长期价值的信息：用户事实、偏好、重要事件、关系等。
忽略日常闲聊、临时性的问候、无信息量的内容。
如果没有值得记住的信息，返回空数组 []。

返回 JSON 数组，每条包含：
- type: "fact" | "preference" | "event"
- content: 第三人称描述的记忆事实，如"用户叫张三，今年30岁"
- importance: 0.0-1.0 的重要性评分，越高越重要

对话内容：
{messages}

仅返回 JSON 数组，不要其他内容。"""


CONFLICT_PROMPT = """你是一个记忆冲突处理系统。判断新记忆与已有记忆之间的关系，决定如何处理。

已有记忆：
{existing}

新提取的记忆：
{new_memories}

对每条新记忆，返回一个操作：
- add: 全新的信息，需要添加
  {{"action": "add", "new_memory": {{"content": "...", "importance": 0.8}}}}
- update: 新信息替代/修正旧信息（如地址变更、年龄更新）
  {{"action": "update", "old_id": "mem_001", "new_content": "...", "new_importance": 0.9}}
- skip: 重复信息，或旧信息比新信息更详细，无需操作
  {{"action": "skip", "reason": "重复"}}
- delete: 旧信息已过时且无需替换
  {{"action": "delete", "old_id": "mem_001", "reason": "已过时"}}

返回 JSON 数组，仅返回 JSON。"""


class MemoryExtractor:
    def __init__(self, settings: Settings, llm_client, embedding_client):
        self._settings = settings
        self._llm = llm_client
        self._embedding = embedding_client

    async def extract_memories(self, messages: list[dict]) -> list[dict]:
        """Extract long-term memories from conversation messages."""
        text = messages_to_text(messages)
        prompt = EXTRACTION_PROMPT.format(messages=text)

        try:
            items = await self._llm.chat_json(
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            return []

        if not isinstance(items, list):
            return []

        # Filter by importance threshold
        threshold = self._settings.mem_importance_threshold
        return [
            item for item in items
            if item.get("importance", 0) >= threshold
        ]

    async def resolve_conflicts(
        self, new_memories: list[dict], existing_memories: list[dict]
    ) -> list[dict]:
        """Determine what to do with each new memory (add/update/skip/delete)."""
        if not new_memories:
            return []

        existing_str = json.dumps(
            [
                {"id": m.get("_id", ""), "content": m.get("memory", ""),
                 "importance": m.get("importance", 0)}
                for m in existing_memories
            ],
            ensure_ascii=False,
        )
        new_str = json.dumps(new_memories, ensure_ascii=False)

        prompt = CONFLICT_PROMPT.format(
            existing=existing_str, new_memories=new_str
        )

        try:
            actions = await self._llm.chat_json(
                messages=[{"role": "user", "content": prompt}],
            )
            if not isinstance(actions, list):
                return []
            return actions
        except Exception:
            # On failure, default to add all
            return [
                {"action": "add", "new_memory": m} for m in new_memories
            ]

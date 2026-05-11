import json
import logging
from datetime import datetime

from memory_system.clients.llm_client import LLMClient
from memory_system.prompts import USER_MEMORY_EXTRACTION_PROMPT, DEFAULT_UPDATE_MEMORY_PROMPT
from memory_system.utils.text_utils import messages_to_text

logger = logging.getLogger(__name__)


class MemoryExtractor:
    """Extract facts from conversation using mem0's prompt + HTTP LLM."""

    def __init__(self, llm_client: LLMClient):
        self._llm = llm_client

    async def extract(self, messages: list[dict]) -> list[str]:
        """Extract user facts from conversation messages.

        Returns a list of fact strings (e.g. ['Name is Bob', 'Lives in Beijing']).
        Returns empty list if nothing relevant found.
        """
        text = messages_to_text(messages)
        if not text.strip():
            return []

        system_prompt = USER_MEMORY_EXTRACTION_PROMPT.replace(
            "{date}", datetime.now().strftime("%Y-%m-%d")
        )

        try:
            facts = await self._llm.extract_json_field(
                system=system_prompt,
                user=f"Input:\n{text}",
                field="facts",
            )
            logger.info(f"Extracted {len(facts)} facts: {facts}")
            return facts
        except Exception as e:
            logger.error(f"Extract failed: {e}")
            return []

    async def update_memory(
        self,
        old_memories: list[dict],
        new_facts: list[str],
    ) -> list[dict]:
        """Decide ADD/UPDATE/DELETE/NONE actions for new facts against old memories.

        Args:
            old_memories: [{"id": "abc123", "text": "User is a coder"}, ...]
            new_facts: ["User is a software engineer", ...]

        Returns:
            [{"id": "abc123", "text": "User is a software engineer",
              "event": "UPDATE", "old_memory": "User is a coder"}, ...]
        """
        # Build the prompt following mem0's get_update_memory_messages pattern
        if old_memories:
            current_memory_part = f"""
Below is the current content of my memory which I have collected till now. You have to update it in the following format only:

```
{json.dumps(old_memories, ensure_ascii=False)}
```

"""
        else:
            current_memory_part = "\nCurrent memory is empty.\n\n"

        prompt = f"""{DEFAULT_UPDATE_MEMORY_PROMPT}

{current_memory_part}

The new retrieved facts are mentioned in the triple backticks. You have to analyze the new retrieved facts and determine whether these facts should be added, updated, or deleted in the memory.

```
{json.dumps(new_facts, ensure_ascii=False)}
```

You must return your response in the following JSON structure only:

{{
    "memory" : [
        {{
            "id" : "<ID of the memory>",
            "text" : "<Content of the memory>",
            "event" : "<Operation to be performed>",
            "old_memory" : "<Old memory content>"
        }},
        ...
    ]
}}

Follow the instruction mentioned below:
- Do not return anything from the custom few shot prompts provided above.
- If the current memory is empty, then you have to add the new retrieved facts to the memory.
- You should return the updated memory in only JSON format as shown below. The memory key should be the same if no changes are made.
- If there is an addition, generate a new key and add the new memory corresponding to it.
- If there is a deletion, the memory key-value pair should be removed from the memory.
- If there is an update, the ID key should remain the same and only the value needs to be updated.

Do not return anything except the JSON format."""

        raw = await self._llm.generate_json(
            system="You are a smart memory manager.",
            user=prompt,
            temperature=0.1,
            max_tokens=2000,
        )

        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
            actions = result.get("memory", [])
            logger.info(
                f"Update memory decisions: {len(actions)} actions — "
                + ", ".join(f"{a.get('event')}:{a.get('id','?')}" for a in actions)
            )
            return actions
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to parse update_memory response: {e}\nRaw: {raw}")
            # Fallback: ADD all facts as new
            return [
                {"id": "", "text": f, "event": "ADD", "old_memory": ""}
                for f in new_facts
            ]

def extract_text(content: str | list[dict]) -> str:
    """Extract plain text from content field (string or multimodal array)."""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if item.get("type") == "input_text" and item.get("text"):
            parts.append(item["text"])
    return " ".join(parts)


def messages_to_text(messages: list[dict]) -> str:
    """Convert messages list to a single text block for embedding/LLM."""
    lines = []
    for msg in messages:
        role = msg["role"]
        text = extract_text(msg.get("content", ""))
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def get_last_user_query(messages: list[dict]) -> str:
    """Extract the text of the last user message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return extract_text(msg.get("content", ""))
    return ""


def flatten_messages(messages: list[dict]) -> list[dict]:
    """Convert OpenAI content-block messages to plain role/content strings.

    mem0's parse_vision_messages wrongly treats any content-as-list as images,
    so we convert to plain strings before passing to mem0.
    """
    return [
        {"role": m["role"], "content": extract_text(m.get("content", ""))}
        for m in messages
    ]


def messages_to_query(messages: list[dict]) -> str:
    """Build query text for embedding search.

    If there are assistant messages, use assistant content (agent-centric).
    Otherwise use user content (user-centric).
    Falls back to all messages if target role yields no text.
    """
    has_assistant = any(m.get("role") == "assistant" for m in messages)
    target_role = "assistant" if has_assistant else "user"
    lines = []
    for msg in messages:
        if msg.get("role") == target_role:
            text = extract_text(msg.get("content", ""))
            if text:
                lines.append(text)
    return "\n".join(lines) if lines else messages_to_text(messages)

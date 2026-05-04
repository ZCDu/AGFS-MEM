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

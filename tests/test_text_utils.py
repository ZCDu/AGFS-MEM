from memory_system.utils.text_utils import extract_text, messages_to_text


def test_extract_text_from_string():
    assert extract_text("hello world") == "hello world"


def test_extract_text_from_content_list():
    content = [
        {"type": "input_text", "text": "what's in this image?"},
        {
            "type": "input_image",
            "image_url": "https://example.com/image.jpg",
        },
    ]
    result = extract_text(content)
    assert result == "what's in this image?"


def test_extract_text_empty_list():
    assert extract_text([]) == ""


def test_extract_text_only_image():
    content = [
        {
            "type": "input_image",
            "image_url": "https://example.com/image.jpg",
        },
    ]
    assert extract_text(content) == ""


def test_messages_to_text():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    result = messages_to_text(messages)
    assert result == "user: hello\nassistant: hi there"


def test_messages_to_text_with_multimodal():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what's this?"},
                {"type": "input_image", "image_url": "http://x.com/img.png"},
            ],
        },
        {"role": "assistant", "content": "that's a cat"},
    ]
    result = messages_to_text(messages)
    assert result == "user: what's this?\nassistant: that's a cat"


def test_get_last_user_query():
    from memory_system.utils.text_utils import get_last_user_query

    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second question"},
    ]
    assert get_last_user_query(messages) == "second question"


def test_get_last_user_query_multimodal():
    from memory_system.utils.text_utils import get_last_user_query

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "last question"},
                {"type": "input_image", "image_url": "http://x.com/img.png"},
            ],
        },
    ]
    assert get_last_user_query(messages) == "last question"

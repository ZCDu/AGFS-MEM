"""
Chat endpoint tests.

The interesting property is retrieval: the assistant is only useful if a
question actually pulls the right entities out of memory.
"""

from __future__ import annotations

import pytest

from app.api.routes_chat import _memory_context


@pytest.fixture()
def seeded(client):
    client.put("/v1/users/demo/wiki", json={
        "type": "person", "title": "Alice Chen", "aliases": ["Alice"],
        "summary_append": "Staff engineer on the retrieval team."})
    client.put("/v1/users/demo/wiki", json={
        "type": "project", "title": "Orion",
        "summary_append": "Internal search platform in beta."})
    return client


def _store():
    from app.deps import get_graph_store
    return get_graph_store()


def test_short_questions_still_retrieve(seeded):
    """Regression. Retrieval reused the assessor's STORAGE thresholds, whose
    length gate returns early with related=[] — so every question under twelve
    words retrieved nothing, which is most questions.

    Whether text is worth keeping and what it refers to are different
    questions, and only the second one matters here.
    """
    _, used = _memory_context(_store(), "demo", "Who is Alice?")
    assert [u["wiki_id"] for u in used] == ["person/alice-chen"], \
        "a four-word question must still match"


def test_retrieval_finds_multiple_entities_and_builds_context(seeded):
    ctx, used = _memory_context(
        _store(), "demo", "What is Alice Chen working on with Orion?")
    ids = {u["wiki_id"] for u in used}
    assert ids == {"person/alice-chen", "project/orion"}
    assert "Alice Chen" in ctx and "Orion" in ctx
    assert ctx.startswith("Relevant memory:")


def test_unrelated_message_retrieves_nothing(seeded):
    """Retrieval must not pad the prompt with irrelevant entities."""
    ctx, used = _memory_context(_store(), "demo", "tell me about the weather")
    assert used == [] and ctx == ""


def test_chat_without_an_api_key_is_503_not_500(seeded, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    import app.config as cfg
    cfg._settings = None
    r = seeded.post("/v1/users/demo/chat",
                    json={"messages": [{"role": "user", "content": "hello there"}]})
    assert r.status_code == 503
    assert "API key" in r.json()["detail"]


def test_chat_rejects_an_empty_conversation(seeded):
    r = seeded.post("/v1/users/demo/chat",
                    json={"messages": [{"role": "user", "content": "   "}]})
    assert r.status_code in (422, 503)


def test_chat_ui_is_served_and_parses(client):
    r = client.get("/chat")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]

    import re
    html = r.text
    defined = {i for i in re.findall(r'id="([^"]+)"', html) if "${" not in i}
    referenced = set(re.findall(r'\$\("([^"]+)"\)', html))
    assert not referenced - defined, f"dead $() refs: {sorted(referenced - defined)}"

    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    seen: dict[str, int] = {}
    for line in js.split("\n"):
        m = re.match(r"(let|const)\s+([A-Za-z_$][\w$]*)\s*=", line)
        if m:
            seen[m.group(2)] = seen.get(m.group(2), 0) + 1
    assert not {k: v for k, v in seen.items() if v > 1}, \
        "duplicate top-level declaration is a fatal SyntaxError"


def test_chat_asks_for_prose_and_extraction_asks_for_json():
    """The client hardcoded response_format=json_object, which is right for
    extraction and wrong for chat: with it on, DeepSeek returns a JSON object
    where the user expects a sentence.

    Asserts on the request body actually sent, since a signature check alone
    would not have caught the original bug.
    """
    import json
    from app.extract.llm import LLMClient

    sent = {}

    class Recorder(LLMClient):
        def _open(self, req):          # not called; we intercept below
            raise AssertionError

    client = LLMClient(api_key="k", base_url="http://stub", model="m")

    import urllib.request
    real = urllib.request.urlopen

    class FakeResp:
        status = 200
        def read(self): return json.dumps(
            {"choices": [{"message": {"content": "hello"}}], "usage": {}}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=None):
        sent["body"] = json.loads(req.data)
        return FakeResp()

    urllib.request.urlopen = fake
    try:
        client.complete("sys", "usr", json_mode=False)
        assert "response_format" not in sent["body"], \
            "chat must not request JSON mode or replies come back as JSON"

        client.complete("sys", "usr", json_mode=True)
        assert sent["body"]["response_format"] == {"type": "json_object"}, \
            "extraction relies on JSON mode"
    finally:
        urllib.request.urlopen = real


def test_chat_ui_applies_operations_not_raw_text(client):
    """/extract/apply takes the reviewed operations, not a transcript.

    Sending {"text": ...} produced a 422 whose `input` field echoed the whole
    conversation back — so a long chat produced an error message thousands of
    characters long, which broke the panel it was rendered into.

    Applying the reviewed operations is also the correct semantics: what gets
    written is exactly what was shown, not a fresh extraction.
    """
    html = client.get("/chat").text
    apply_call = html[html.index('$("apply").onclick'):]
    apply_call = apply_call[:apply_call.index("};")]
    assert "operations:" in apply_call, "apply must send operations"
    assert "text:" not in apply_call, "apply must not send the transcript"


def test_apply_rejects_a_transcript_payload(client):
    """The shape the UI used to send. Kept as a test so the error stays a
    clean 422 rather than becoming a 500."""
    r = client.post("/v1/users/demo/extract/apply", json={"text": "anything"})
    assert r.status_code == 422


def test_long_errors_cannot_break_the_layout(client):
    """A validation error can be thousands of characters with no spaces to
    wrap on. Without these rules it runs off screen and takes the layout with
    it, which is what made the failure impossible to read."""
    css = client.get("/chat").text
    assert css.count("overflow-wrap:anywhere") >= 2
    assert "max-height:40vh" in css, "the toast must be height-capped"
    assert ".errbox" in css, "errors need a scrollable, wrapped container"


def test_both_uis_load_the_same_sign_in_widget(client):
    """The two pages previously had separate sign-in implementations and
    drifted: the graph editor grew a modal, the chat page never did, so chat
    users had to fetch a token with curl and paste it in by hand.

    One shared file cannot drift from itself.
    """
    assert client.get("/static/auth.js").status_code == 200
    for page in ("/chat", "/gui"):
        assert "/static/auth.js" in client.get(page).text, f"{page} must load the widget"


def test_sign_in_widget_has_no_inline_display_on_a_hidden_element(client):
    """The overlay bug, twice over. `[hidden]` is a user-agent rule; an inline
    `display` outranks it and leaves a full-screen modal permanently on top of
    the page, swallowing every click including the button that opens it."""
    js = client.get("/static/auth.js").text
    assert "#ma_box[hidden] { display:none; }" in js
    assert 'wrap.hidden = true' in js
    assert "style=\"" not in js.split("innerHTML")[1][:400] or "display" not in \
        js.split("innerHTML")[1][:400], "modal markup must not set display inline"


def test_chat_retrieves_entities_from_storage(seeded):
    """The whole point: the assistant answers from what is actually stored.

    Reads real entity files through the configured backend — on a deployment
    that is S3 via mirage, and the same code path either way.
    """
    ctx, used = _memory_context(_store(), "demo", "Tell me about Orion")
    assert [u["wiki_id"] for u in used] == ["project/orion"]
    assert "Internal search platform in beta." in ctx, \
        "the stored summary must reach the prompt"


def test_lowercase_questions_retrieve(seeded):
    """The bug people actually hit. assess() returns EARLY when text has no
    proper nouns and no importance markers — before _link() runs — so
    `related` came back empty.

    That guard is correct for deciding what to STORE. It is wrong for
    retrieval: "who is alice?" is lowercase, has no proper nouns, and is
    exactly the question a person types. The chatbot answered "I have no
    records about Alice" while the entity sat in storage.
    """
    for question in ("who is alice?", "tell me about alice", "what is orion"):
        _, used = _memory_context(_store(), "demo", question)
        assert used, f"{question!r} retrieved nothing"


def test_capitalisation_does_not_change_what_is_retrieved(seeded):
    lower = {u["wiki_id"] for u in _memory_context(_store(), "demo", "who is alice?")[1]}
    upper = {u["wiki_id"] for u in _memory_context(_store(), "demo", "Who is Alice?")[1]}
    assert lower == upper == {"person/alice-chen"}


def test_greetings_still_retrieve_nothing(seeded):
    """link_only must not turn retrieval into a match-everything filter."""
    for noise in ("hello?", "thanks!", "who am i?"):
        _, used = _memory_context(_store(), "demo", noise)
        assert used == [], f"{noise!r} should not match anything"


def test_link_only_does_not_affect_storage_decisions(client):
    """The early exits still apply to the assessor's normal use, or chatter
    would start being stored."""
    from app.verify.assessor import ConversationAssessor
    store = _store()
    assessor = ConversationAssessor(store)
    assert assessor.assess("demo", "who is alice?").decision == "skip"
    assert assessor.assess("demo", "hello there thanks a lot").decision == "skip"

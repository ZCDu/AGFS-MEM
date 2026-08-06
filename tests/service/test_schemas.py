import pytest

from short_term_memory.service.schemas import (
    EffectiveMemoryConfig,
    MemoryReadRequest,
    MemoryWriteRequest,
)


def test_write_schema_accepts_all_four_content_types() -> None:
    request = MemoryWriteRequest.model_validate(
        {
            "user_id": "u1",
            "session_id": "s1",
            "events": [
                {
                    "event_id": f"e-{kind}",
                    "role": "user",
                    "content_type": kind,
                    "content": f"original-{kind}",
                    "metadata": {},
                }
                for kind in ("conversation", "code", "document", "skill")
            ],
        }
    )

    assert [event.content_type.value for event in request.events] == [
        "conversation",
        "code",
        "document",
        "skill",
    ]


def test_write_schema_excludes_server_generated_event_fields() -> None:
    with pytest.raises(ValueError):
        MemoryWriteRequest.model_validate(
            {
                "user_id": "u1",
                "session_id": "s1",
                "events": [
                    {
                        "event_id": "e1",
                        "role": "user",
                        "content_type": "conversation",
                        "content": "original",
                        "metadata": {},
                        "sequence": 1,
                    }
                ],
            }
        )


def test_read_request_accepts_optional_effective_config() -> None:
    request = MemoryReadRequest(
        user_id="u1",
        session_id="s1",
        history_turns=10,
        include_effective_config=True,
    )

    assert request.include_effective_config is True


def test_effective_config_never_contains_secrets() -> None:
    assert set(EffectiveMemoryConfig.model_fields) == {
        "history_turns",
        "redis_ttl_seconds",
        "ccr_ttl_seconds",
        "journal_retention_days",
        "trigger_ratio",
        "policy_version",
    }

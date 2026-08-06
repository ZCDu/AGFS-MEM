import pytest

from short_term_memory.service.schemas import (
    EffectiveMemoryConfig,
    HeadroomProxyContext,
    MemoryReadResponse,
    MemoryReadState,
    MemoryReadRequest,
    MemoryWriteResponse,
    MemoryWriteRequest,
    ReadTiming,
    WriteTiming,
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


def test_write_response_dumps_the_approved_nullable_sequence_contract() -> None:
    response = MemoryWriteResponse(
        request_id="req-1",
        accepted=True,
        sequence_from=None,
        sequence_through=None,
        duplicate_event_ids=["event-1"],
        compression_queued=False,
        policy_version="v1",
        timing_ms=WriteTiming(total=42.6, redis=8.1, journal=28.4, queue=1.2),
    )

    assert response.model_dump(mode="json") == {
        "request_id": "req-1",
        "accepted": True,
        "sequence_from": None,
        "sequence_through": None,
        "duplicate_event_ids": ["event-1"],
        "compression_queued": False,
        "policy_version": "v1",
        "timing_ms": {"total": 42.6, "redis": 8.1, "journal": 28.4, "queue": 1.2},
    }


def test_read_response_dumps_the_approved_optional_config_contract() -> None:
    response = MemoryReadResponse(
        request_id="req-2",
        messages=[{"role": "user", "content": "recent original message"}],
        memory=MemoryReadState(
            compressed_through_sequence=100,
            latest_sequence=101,
            source="redis",
            compression_segments=1,
        ),
        headroom=HeadroomProxyContext(
            proxy_url="http://headroom:8787/v1",
            scope_headers={"x-headroom-user-id": "opaque-value"},
        ),
        effective_config=None,
        timing_ms=ReadTiming(total=31.5, redis=12.2, recovery=0.0, assembly=3.1),
    )

    assert response.model_dump(mode="json") == {
        "request_id": "req-2",
        "messages": [{"role": "user", "content": "recent original message"}],
        "memory": {
            "compressed_through_sequence": 100,
            "latest_sequence": 101,
            "source": "redis",
            "compression_segments": 1,
        },
        "headroom": {
            "proxy_url": "http://headroom:8787/v1",
            "scope_headers": {"x-headroom-user-id": "opaque-value"},
        },
        "effective_config": None,
        "timing_ms": {"total": 31.5, "redis": 12.2, "recovery": 0.0, "assembly": 3.1},
    }

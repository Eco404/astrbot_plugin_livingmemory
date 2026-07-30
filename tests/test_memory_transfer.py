"""Tests for portable Timeline import/export formats."""

import json

from astrbot_plugin_livingmemory.core.memory_transfer import (
    memory_import_key,
    parse_transfer_content,
    portable_metadata,
    serialize_transfer_csv,
    serialize_transfer_json,
)


def _record():
    return {
        "original_id": 7,
        "content": "User prefers concise answers",
        "importance": 0.8,
        "session_id": "session-1",
        "persona_id": "persona-1",
        "metadata": {
            "topics": ["preference"],
            "key_facts": ["concise answers"],
            "memory_type": "PREFERENCE",
            "memory_uid": "must-not-round-trip",
            "memory_space_id": "local-only",
            "memory_layer": "topic",
            "revision": 9,
            "importance_revision": 6,
            "importance_policy_version": 1,
            "update_history": [{"mode": "remote"}],
            "topic_sync_state": "synced",
            "source_window": {"first_message_id": 1, "last_message_id": 2},
        },
        "source_messages": [
            {
                "id": 1,
                "session_id": "session-1",
                "role": "user",
                "content": "Please keep answers concise",
                "sender_id": "user-1",
                "timestamp": 1.0,
                "metadata": {},
            }
        ],
    }


def test_native_json_round_trip_preserves_semantics_not_local_identity():
    content = serialize_transfer_json([_record()], "2026-07-29T00:00:00+00:00")

    entries, errors = parse_transfer_content(content, "json")

    assert errors == []
    entry = entries[0]
    assert entry.content == "User prefers concise answers"
    assert entry.importance == 0.8
    assert entry.session_id == "session-1"
    assert entry.metadata["memory_type"] == "PREFERENCE"
    assert entry.metadata["imported_from_id"] == 7
    assert "memory_uid" not in entry.metadata
    assert "memory_space_id" not in entry.metadata
    assert "revision" not in entry.metadata
    assert "memory_layer" not in entry.metadata
    assert "importance_revision" not in entry.metadata
    assert "importance_policy_version" not in entry.metadata
    assert "update_history" not in entry.metadata
    assert "topic_sync_state" not in entry.metadata
    assert entry.metadata["imported_source_window"]["first_message_id"] == 1
    assert entry.source_messages[0]["content"] == "Please keep answers concise"


def test_csv_round_trip_preserves_metadata_and_source():
    content = serialize_transfer_csv([_record()])

    entries, errors = parse_transfer_content(content, "csv")

    assert errors == []
    entry = entries[0]
    assert entry.metadata["topics"] == ["preference"]
    assert entry.metadata["key_facts"] == ["concise answers"]
    assert "memory_uid" not in entry.metadata
    assert entry.source_messages[0]["role"] == "user"


def test_csv_bom_formula_escape_and_restore():
    record = _record()
    record["content"] = '=HYPERLINK("https://example.invalid")'
    record["session_id"] = "@external-session"
    content = "\ufeff" + serialize_transfer_csv([record])

    assert "'=HYPERLINK" in content
    assert "'@external-session" in content
    entries, errors = parse_transfer_content(content, "csv")
    assert errors == []
    assert entries[0].content == '=HYPERLINK("https://example.invalid")'
    assert entries[0].session_id == "@external-session"


def test_external_long_and_short_term_collections_are_normalized():
    payload = {
        "long_term_memories": [
            {
                "summary": "Deployment happens Friday",
                "importance": 8,
                "topics": ["release"],
            }
        ],
        "short_term_memories": [
            [
                {"sender": "human", "text": "The code is alpha"},
                {"sender": "ai", "text": "I will remember that"},
            ]
        ],
    }

    entries, errors = parse_transfer_content(json.dumps(payload), "json")

    assert errors == []
    assert entries[0].content == "Deployment happens Friday"
    assert entries[0].importance == 0.8
    assert entries[1].requires_summary is True
    assert [item["role"] for item in entries[1].source_messages] == [
        "user",
        "assistant",
    ]


def test_session_mapping_invalid_entry_and_duplicate_keys():
    payload = {
        "short_term_memories": {
            "external-session": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]
        }
    }
    entries, errors = parse_transfer_content(json.dumps(payload), "json")
    assert errors == []
    assert entries[0].session_id == "external-session"

    entries, errors = parse_transfer_content(
        json.dumps([{"summary": "valid"}, {"messages": [{"role": "user"}]}]),
        "json",
    )
    assert [entry.content for entry in entries] == ["valid"]
    assert errors[0]["index"] == 1

    assert memory_import_key("one   two", "s1", "p1") == memory_import_key(
        " one two ", "s1", "p1"
    )
    assert memory_import_key("one two", "s1", "p1") != memory_import_key(
        "one two", "s2", "p1"
    )


def test_object_mapping_imports_each_memory_once():
    entries, errors = parse_transfer_content(
        json.dumps(
            {
                "remote-1": {"summary": "first"},
                "remote-2": "second",
            }
        ),
        "json",
    )

    assert errors == []
    assert [entry.content for entry in entries] == ["first", "second"]
    assert [entry.metadata["imported_from_id"] for entry in entries] == [
        "remote-1",
        "remote-2",
    ]


def test_portable_metadata_does_not_mutate_input():
    source = {"memory_uid": "u1", "topics": ["x"], "status": "archived"}
    cleaned = portable_metadata(source)
    assert cleaned == {"topics": ["x"]}
    assert source["memory_uid"] == "u1"

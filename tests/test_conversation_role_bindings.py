from astrbot_plugin_livingmemory.core.models.conversation_models import (
    Message,
    build_role_bindings,
)


def test_assistant_role_binding_prefers_persona_name_over_numeric_sender_name():
    bindings = build_role_bindings(
        [
            Message(
                1,
                "qq:FriendMessage:user-1",
                "assistant",
                "回复",
                "123456789",
                "123456789",
                platform="qq",
                metadata={"persona_name": "唯", "is_bot_message": True},
            )
        ],
        persona_id="persona-1",
    )

    actor = bindings["actors"][0]
    assert actor["persona_name"] == "唯"
    assert actor["observed_names"][0] == "唯"
    assert actor["observed_names"] == ["唯"]
    assert actor["sender_id"] == "123456789"


def test_private_assistant_sender_collision_uses_persona_actor_anchor():
    bindings = build_role_bindings(
        [
            Message(
                1,
                "QQ2949374079:FriendMessage:1141337347",
                "user",
                "刚理完头发",
                "1141337347",
                "空雨",
                platform="qq",
            ),
            Message(
                2,
                "QQ2949374079:FriendMessage:1141337347",
                "assistant",
                "想开电脑写一小段",
                "1141337347",
                "空雨",
                platform="qq",
                metadata={"is_bot_message": True, "persona_name": "唯"},
            ),
        ],
        persona_id="persona-1",
    )

    assert bindings["message_actor_ids"]["M1"] == "qq:human:1141337347"
    assert bindings["message_actor_ids"]["M2"] == "qq:assistant:persona:persona-1"
    assert bindings["narrator_actor_id"] == "qq:assistant:persona:persona-1"
    assert "assistant_sender_matches_private_peer" in bindings["ambiguity_flags"]
    assistant = next(
        actor for actor in bindings["actors"] if actor["actor_type"] == "assistant"
    )
    assert assistant["observed_names"] == ["唯"]

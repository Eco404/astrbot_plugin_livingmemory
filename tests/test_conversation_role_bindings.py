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
                metadata={"persona_name": "测试助手", "is_bot_message": True},
            )
        ],
        persona_id="persona-1",
    )

    actor = bindings["actors"][0]
    assert actor["persona_name"] == "测试助手"
    assert actor["observed_names"][0] == "测试助手"
    assert actor["observed_names"] == ["测试助手"]
    assert actor["sender_id"] == "123456789"


def test_private_assistant_sender_collision_uses_persona_actor_anchor():
    bindings = build_role_bindings(
        [
            Message(
                1,
                "QQ20000001:FriendMessage:10000001",
                "user",
                "刚整理完资料",
                "10000001",
                "示例甲",
                platform="qq",
            ),
            Message(
                2,
                "QQ20000001:FriendMessage:10000001",
                "assistant",
                "准备编写测试说明",
                "10000001",
                "示例甲",
                platform="qq",
                metadata={"is_bot_message": True, "persona_name": "测试助手"},
            ),
        ],
        persona_id="persona-1",
    )

    assert bindings["message_actor_ids"]["M1"] == "qq:human:10000001"
    assert bindings["message_actor_ids"]["M2"] == "qq:assistant:persona:persona-1"
    assert bindings["narrator_actor_id"] == "qq:assistant:persona:persona-1"
    assert "assistant_sender_matches_private_peer" in bindings["ambiguity_flags"]
    assistant = next(
        actor for actor in bindings["actors"] if actor["actor_type"] == "assistant"
    )
    assert assistant["observed_names"] == ["测试助手"]

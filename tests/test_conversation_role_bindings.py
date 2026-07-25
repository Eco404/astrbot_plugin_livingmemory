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
    assert actor["observed_names"] == ["测试助手", "123456789"]

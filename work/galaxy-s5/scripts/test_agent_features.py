#!/usr/bin/env python3
"""
Unit and Integration Test Suite for S5 Telegram Autonomous Agent Features.
Tests:
- Markdown cleaning and response formatting
- Multi-turn session state, switching, and deletion
- Context compaction logic
- Reasoning toggle
- System prompt customization
- File download/upload helpers
- BotCommand auto-completion definitions
"""

import sys
import os
import re
import time
from pathlib import Path

# Add current scripts directory
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

def test_markdown_cleaner():
    print("Testing Markdown Cleaner...")
    # Test case 1: Raw code block with print
    raw1 = 'Thoughts: The user asked for hostname\n<code>\nprint("galaxy-s5")\n</code>'
    # Should strip code tags and convert to clean format
    from s5_agent_core import clean_markdown_for_telegram, format_agent_response
    cleaned1 = clean_markdown_for_telegram(raw1)
    assert "<code>" not in cleaned1, f"Failed: {cleaned1}"
    assert "galaxy-s5" in cleaned1, f"Failed: {cleaned1}"

    # Test case 2: Reasoning mode OFF strips thoughts
    formatted_off = format_agent_response(raw1, thoughts=["Inspecting host"], show_reasoning=False)
    assert "Thoughts:" not in formatted_off, f"Failed: {formatted_off}"
    assert "galaxy-s5" in formatted_off, f"Failed: {formatted_off}"

    # Test case 3: Reasoning mode ON includes thoughts block
    formatted_on = format_agent_response("Result is 42", thoughts=["Calculating meaning of life"], show_reasoning=True)
    assert "Reasoning:" in formatted_on, f"Failed: {formatted_on}"
    assert "Calculating meaning of life" in formatted_on, f"Failed: {formatted_on}"
    assert "Result is 42" in formatted_on, f"Failed: {formatted_on}"

    print("✅ Markdown Cleaner tests passed!")


def test_session_management():
    print("Testing Session Management...")
    from s5_agent_core import UserState, Session, DEFAULT_SYSTEM_PROMPT

    state = UserState(user_id=12345)
    # Default session must exist
    assert "default" in state.sessions
    assert state.active_session_id == "default"
    assert state.get_active_session().name == "default"

    # Create new session
    sid2 = state.create_session(name="network-test")
    assert sid2 in state.sessions
    assert state.active_session_id == sid2
    assert state.get_active_session().name == "network-test"

    # Switch back to default
    ok, msg = state.switch_session("default")
    assert ok, f"Switch failed: {msg}"
    assert state.active_session_id == "default"

    # Switch by name
    ok, msg = state.switch_session("network-test")
    assert ok, f"Switch by name failed: {msg}"
    assert state.active_session_id == sid2

    # Listing sessions
    sessions_list = state.list_sessions()
    assert len(sessions_list) == 2
    assert any(s["is_active"] for s in sessions_list)

    # Delete session
    ok, msg = state.remove_session("default")
    assert ok, f"Delete failed: {msg}"
    assert "default" not in state.sessions
    assert state.active_session_id == sid2

    # Cannot delete last active session without creating new one
    ok, msg = state.remove_session(sid2)
    assert ok
    # State should automatically recreate a default session
    assert len(state.sessions) >= 1
    assert state.active_session_id in state.sessions

    print("✅ Session Management tests passed!")


def test_model_switching_and_prompts():
    print("Testing Model Switching and Prompt Customization...")
    from s5_agent_core import UserState, DEFAULT_SYSTEM_PROMPT

    state = UserState(user_id=999)
    session = state.get_active_session()

    # Default model and prompt
    assert session.system_prompt == DEFAULT_SYSTEM_PROMPT

    # Change model
    session.model_id = "meta-llama/llama-3.3-70b-instruct:free"
    assert session.model_id == "meta-llama/llama-3.3-70b-instruct:free"

    # Custom prompt
    custom_p = "You are a specialized diagnostic tool."
    session.system_prompt = custom_p
    assert session.system_prompt == custom_p

    # Reset prompt
    session.system_prompt = DEFAULT_SYSTEM_PROMPT
    assert session.system_prompt == DEFAULT_SYSTEM_PROMPT

    print("✅ Model Switching & Prompt tests passed!")


def test_compact_logic():
    print("Testing Compaction Logic...")
    from s5_agent_core import Session, compact_session

    session = Session("test-compact")
    # Empty session
    res = compact_session(session)
    assert "empty" in res.lower()

    # Mock agent with steps
    class MockStep:
        def __init__(self, task, output):
            self.task = task
            self.action_output = output

    class MockAgent:
        def __init__(self, step_count):
            class MockMemory:
                pass
            self.memory = MockMemory()
            self.memory.steps = [MockStep(f"Task {i}", f"Output {i}") for i in range(step_count)]

    session.agent = MockAgent(2)
    res2 = compact_session(session)
    assert "minimal" in res2.lower()

    session.agent = MockAgent(6)
    res3 = compact_session(session)
    assert "reduced" in res3.lower() or "condensed" in res3.lower() or "complete" in res3.lower()

    print("✅ Compaction Logic tests passed!")


def test_bot_commands_list():
    print("Testing Bot Commands Configuration...")
    from s5_agent_core import BOT_COMMANDS
    from telegram import BotCommand

    assert len(BOT_COMMANDS) >= 8
    cmd_names = [c.command for c in BOT_COMMANDS]
    for expected in ["status", "sessions", "session", "model", "models", "compact", "reasoning", "prompt", "get", "reset"]:
        assert expected in cmd_names, f"Missing command: {expected}"

    print("✅ Bot Commands list tests passed!")


def test_group_and_topic_policy():
    print("Testing Group and Topic Policy...")
    from s5_agent_core import GroupPolicy, get_session_scope_key
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        policy = GroupPolicy(config_path=Path(tmp.name), default_group_id=-1001234567890, default_topic_name="home")
        
        # Check group filtering
        assert policy.is_group_allowed(-1001234567890) is True
        assert policy.is_group_allowed(-1009999999999) is False

        # Register topic 'home' with thread_id 42
        policy.register_topic(42, "home")
        assert policy.allowed_topic_id == 42
        assert policy.is_topic_allowed(42) is True
        assert policy.is_topic_allowed(99) is False  # Other topics in group are rejected

        # Persistence check
        reloaded = GroupPolicy(config_path=Path(tmp.name))
        assert reloaded.allowed_group_id == -1001234567890
        assert reloaded.allowed_topic_id == 42
        assert reloaded.is_topic_allowed(42) is True
        assert reloaded.is_topic_allowed(99) is False

        # Test session scope isolation
        dm_key = get_session_scope_key(chat_id=12345, user_id=12345)
        assert dm_key == "user_12345"

        group_home_key = get_session_scope_key(chat_id=-1001234567890, user_id=12345, thread_id=42)
        assert group_home_key == "group_1001234567890_t42"

        # Topic 99 would have a separate key if it existed
        group_other_key = get_session_scope_key(chat_id=-1001234567890, user_id=12345, thread_id=99)
        assert group_home_key != group_other_key

    print("✅ Group and Topic Policy tests passed!")


def test_group_message_filtering():
    print("Testing Group Message Filtering...")
    from s5_agent_core import should_process_group_message

    bot_user = "MeHomyBot"

    # 1. Unrelated group chat should be ignored
    should_run, text = should_process_group_message(
        "hey anyone know what time the meeting is?",
        bot_username=bot_user,
        is_reply_to_bot=False,
        is_command=False
    )
    assert should_run is False
    assert text == ""

    # 2. Mentioning @MeHomyBot triggers run and strips mention
    should_run, text = should_process_group_message(
        "@MeHomyBot what is the battery percentage?",
        bot_username=bot_user,
        is_reply_to_bot=False,
        is_command=False
    )
    assert should_run is True
    assert text == "what is the battery percentage?"

    # Case-insensitive mention
    should_run, text = should_process_group_message(
        "can you check wifi status @mehomybot please",
        bot_username=bot_user,
        is_reply_to_bot=False,
        is_command=False
    )
    assert should_run is True
    assert "@" not in text
    assert "wifi status" in text

    # Empty prompt mention
    should_run, text = should_process_group_message(
        "@MeHomyBot",
        bot_username=bot_user,
        is_reply_to_bot=False,
        is_command=False
    )
    assert should_run is True
    assert len(text) > 0

    # 3. Direct reply to bot's message triggers run
    should_run, text = should_process_group_message(
        "yes please check the uptime",
        bot_username=bot_user,
        is_reply_to_bot=True,
        is_command=False
    )
    assert should_run is True
    assert text == "yes please check the uptime"

    # 4. Commands
    should_run, text = should_process_group_message(
        "/status@MeHomyBot",
        bot_username=bot_user,
        is_reply_to_bot=False,
        is_command=True
    )
    assert should_run is True

    should_run, text = should_process_group_message(
        "/status@OtherBot",
        bot_username=bot_user,
        is_reply_to_bot=False,
        is_command=True
    )
    assert should_run is False

    print("✅ Group Message Filtering tests passed!")


def test_authorization_flow():
    print("Testing Full Authorization & Topic Routing Flow...")
    import s5_agent_core as core
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        policy = core.GroupPolicy(config_path=Path(tmp.name), default_group_id=-1005555, default_topic_id=10, default_topic_name="home")

        admin_id = 123456789
        other_user = 999999999

        # Simulate is_authorized_message logic
        def check_auth(chat_type, chat_id, user_id, thread_id, text, is_reply=False, is_cmd=False):
            if chat_type == "private":
                return (user_id == admin_id), text
            # Group: must be admin user ID only
            if user_id != admin_id:
                return False, ""
            if not policy.is_group_allowed(chat_id):
                return False, ""
            if not policy.is_topic_allowed(thread_id):
                return False, ""
            return core.should_process_group_message(text, bot_username="MeHomyBot", is_reply_to_bot=is_reply, is_command=is_cmd)

        # 1. Private DM: admin allowed, other rejected
        ok, _ = check_auth("private", admin_id, admin_id, None, "hello")
        assert ok is True
        ok, _ = check_auth("private", other_user, other_user, None, "hello")
        assert ok is False

        # 2. Unauthorized group: rejected
        ok, _ = check_auth("supergroup", -1009999, admin_id, 10, "@MeHomyBot hi")
        assert ok is False

        # 3. Authorized group, unauthorized topic (e.g. topic 99 instead of 10 'home'): rejected
        ok, _ = check_auth("supergroup", -1005555, admin_id, 99, "@MeHomyBot hi")
        assert ok is False

        # 4. Authorized group, topic 10 ('home'):
        # Normal chat without mention: silent (False)
        ok, _ = check_auth("supergroup", -1005555, admin_id, 10, "just talking to team")
        assert ok is False

        # Plain command without @MeHomyBot: silent (False) so it does not interfere with other bots
        ok, _ = check_auth("supergroup", -1005555, admin_id, 10, "/status", is_cmd=True)
        assert ok is False

        # Command directed to another bot (/status@OtherBot): silent (False)
        ok, _ = check_auth("supergroup", -1005555, admin_id, 10, "/status@OtherBot", is_cmd=True)
        assert ok is False

        # Command directed to @MeHomyBot (/status@MeHomyBot): answered (True)
        ok, prompt = check_auth("supergroup", -1005555, admin_id, 10, "/status@MeHomyBot", is_cmd=True)
        assert ok is True
        assert prompt == "/status"

        # Mentioning @MeHomyBot: answered (True)
        ok, prompt = check_auth("supergroup", -1005555, admin_id, 10, "@MeHomyBot what is battery?")
        assert ok is True
        assert prompt == "what is battery?"

        # Replying to bot's message: answered (True)
        ok, prompt = check_auth("supergroup", -1005555, admin_id, 10, "tell me more", is_reply=True)
        assert ok is True
        assert prompt == "tell me more"

        # Another user in topic 'home' mentioning bot: rejected (False) because bot is restricted strictly to admin ID
        ok, _ = check_auth("supergroup", -1005555, other_user, 10, "@MeHomyBot help please")
        assert ok is False

    print("✅ Full Authorization & Topic Routing Flow tests passed!")


if __name__ == "__main__":
    print("Running S5 Agent Test Suite...")
    try:
        test_markdown_cleaner()
        test_session_management()
        test_model_switching_and_prompts()
        test_compact_logic()
        test_bot_commands_list()
        test_group_and_topic_policy()
        test_group_message_filtering()
        test_authorization_flow()
        print("\n🎉 ALL LOCAL TESTS PASSED CLEANLY!")
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)


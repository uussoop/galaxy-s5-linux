#!/usr/bin/env python3
"""
Core models, session state management, and formatting utilities
for the S5 Autonomous Edge Agent.
"""

import re
import time
import json
from pathlib import Path
from typing import Any
from telegram import BotCommand

DEFAULT_SYSTEM_PROMPT = (
    "You are the autonomous AI edge agent running natively on a Samsung Galaxy S5 under postmarketOS Linux (ARMv7, musl libc).\n"
    "You have full root access to the device through your tools: `execute_bash`, `get_battery_report`, `manage_wifi`, and `send_file_to_user`.\n"
    "Guidelines:\n"
    "- When asked to perform a system action or query hardware, write and execute python code calling the appropriate tools.\n"
    "- Always provide direct, helpful, and concise answers.\n"
    "- Call final_answer(...) with your final response to complete the task."
)

BOT_COMMANDS = [
    BotCommand("status", "Instant system, battery & Wi-Fi telemetry"),
    BotCommand("sessions", "List all active conversation sessions"),
    BotCommand("session", "Manage sessions (new, switch, rm)"),
    BotCommand("group", "Whitelist & manage allowed Telegram group"),
    BotCommand("topic", "Whitelist & manage allowed forum topics"),
    BotCommand("model", "View or change current LLM model"),
    BotCommand("models", "List recommended/free models"),
    BotCommand("compact", "Compact/prune session context memory"),
    BotCommand("reasoning", "Toggle thoughts display (on/off)"),
    BotCommand("prompt", "View or set custom system prompt"),
    BotCommand("get", "Download file from device (/get <path>)"),
    BotCommand("reset", "Reset active session memory"),
    BotCommand("new", "Start a fresh conversation session"),
    BotCommand("id", "Show IDs for user, group chat & topic thread"),
    BotCommand("help", "Help and documentation"),
]


def clean_markdown_for_telegram(text: str) -> str:
    """Converts HTML code tags and sanitizes markdown for Telegram."""
    if not text:
        return ""
    # Convert HTML code blocks
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    text = re.sub(r'<pre><code>(.*?)</code></pre>', r'```\n\1\n```', text, flags=re.DOTALL)
    text = re.sub(r'<pre>(.*?)</pre>', r'```\n\1\n```', text, flags=re.DOTALL)
    # Strip unsupported HTML tags
    text = re.sub(r'</?(?:html|body|div|p|span|b|i|u|s|a)[^>]*>', '', text)
    return text.strip()


def format_agent_response(result: Any, thoughts: list[str] | None = None, show_reasoning: bool = False) -> str:
    """Extracts clean response or attaches structured reasoning based on show_reasoning."""
    res_str = str(result).strip()
    res_str = clean_markdown_for_telegram(res_str)

    if not show_reasoning:
        # If output looks like code containing print(...) or final_answer(...), extract clean content
        if "print(" in res_str or "final_answer(" in res_str:
            extracted = re.findall(r'(?:print|final_answer)\(["\'](.*?)["\']\)', res_str, re.DOTALL)
            if extracted:
                res_str = "\n".join(extracted)
        # Strip "Thoughts: ..." if the model dumped reasoning into the text output
        res_str = re.sub(r'(?i)^thoughts?:\s*.*?\n(?=(?:print|final_answer|\n|[A-Z]))', '', res_str, flags=re.DOTALL).strip()
    else:
        if thoughts:
            clean_thoughts = [re.sub(r'\s+', ' ', t).strip() for t in thoughts if t.strip()]
            if clean_thoughts:
                thought_block = "\n".join(f"> {t}" for t in clean_thoughts[-3:])
                res_str = f"💭 *Reasoning:*\n{thought_block}\n\n*Response:*\n{res_str}"

    return res_str.strip() or "(no output returned)"


class Session:
    """Represents a single multi-turn conversation session."""
    def __init__(
        self,
        session_id: str,
        name: str = "",
        model_id: str = "",
        system_prompt: str = "",
        show_reasoning: bool = False
    ):
        self.session_id = session_id
        self.name = name or session_id
        self.model_id = model_id
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.show_reasoning = show_reasoning
        self.created_at = time.time()
        self.last_active = time.time()
        self.message_count = 0
        self.agent: Any = None
        self.recent_thoughts: list[str] = []

    def touch(self):
        self.last_active = time.time()
        self.message_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "model_id": self.model_id,
            "show_reasoning": self.show_reasoning,
            "message_count": self.message_count,
            "last_active": self.last_active,
            "created_at": self.created_at,
        }


class UserState:
    """Manages all conversation sessions and preferences for a single Telegram user."""
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.active_session_id = "default"
        self.sessions: dict[str, Session] = {
            "default": Session("default", name="default")
        }

    def get_active_session(self) -> Session:
        if self.active_session_id not in self.sessions:
            if not self.sessions:
                self.sessions["default"] = Session("default", name="default")
            self.active_session_id = next(iter(self.sessions))
        return self.sessions[self.active_session_id]

    def create_session(self, name: str = "", model_id: str = "") -> str:
        # Generate safe identifier
        sid = f"s{len(self.sessions) + 1}"
        # Ensure unique sid
        counter = len(self.sessions) + 1
        while sid in self.sessions:
            counter += 1
            sid = f"s{counter}"
        
        session_name = name.strip() or sid
        self.sessions[sid] = Session(
            session_id=sid,
            name=session_name,
            model_id=model_id,
            system_prompt=DEFAULT_SYSTEM_PROMPT
        )
        self.active_session_id = sid
        return sid

    def switch_session(self, target: str) -> tuple[bool, str]:
        # Match by session_id first, then by name (case-insensitive)
        if target in self.sessions:
            self.active_session_id = target
            return True, f"✅ Switched to session: `{target}` (*{self.sessions[target].name}*)"
        
        for sid, s in self.sessions.items():
            if s.name.lower() == target.lower():
                self.active_session_id = sid
                return True, f"✅ Switched to session: `{sid}` (*{s.name}*)"

        return False, f"❌ Session `{target}` not found. Type `/sessions` to list available sessions."

    def remove_session(self, target: str) -> tuple[bool, str]:
        matched_sid = None
        if target in self.sessions:
            matched_sid = target
        else:
            for sid, s in self.sessions.items():
                if s.name.lower() == target.lower():
                    matched_sid = sid
                    break

        if not matched_sid:
            return False, f"❌ Session `{target}` not found."

        del self.sessions[matched_sid]
        
        # If deleted active session, pick another or recreate default
        if self.active_session_id == matched_sid:
            if self.sessions:
                self.active_session_id = next(iter(self.sessions))
                switched_msg = f" Switched active session to `{self.active_session_id}`."
            else:
                self.sessions["default"] = Session("default", name="default")
                self.active_session_id = "default"
                switched_msg = " Created a fresh `default` session."
        else:
            switched_msg = ""

        return True, f"🗑️ Deleted session `{matched_sid}`.{switched_msg}"

    def list_sessions(self) -> list[dict[str, Any]]:
        result = []
        for sid, s in sorted(self.sessions.items(), key=lambda x: x[1].last_active, reverse=True):
            d = s.to_dict()
            d["is_active"] = (sid == self.active_session_id)
            result.append(d)
        return result


def compact_session(session: Session) -> str:
    """Condenses session history steps to free context tokens."""
    if not session.agent:
        return "Session memory is empty; nothing to compact."

    memory = getattr(session.agent, "memory", None)
    if not memory or not getattr(memory, "steps", None):
        return "Session memory is empty; nothing to compact."

    steps = memory.steps
    total_steps = len(steps)
    if total_steps <= 2:
        return f"Session memory is already minimal ({total_steps} step(s)). No compaction needed."

    # Extract key interactions from older turns
    older_steps = steps[:-2]
    condensed_points = []
    for i, step in enumerate(older_steps):
        task = getattr(step, "task", None)
        action_output = getattr(step, "action_output", None)
        if task:
            condensed_points.append(f"Turn {i+1} User: {str(task)[:120]}")
        if action_output:
            condensed_points.append(f"Turn {i+1} Result: {str(action_output)[:180]}")

    summary_note = "Condensed historical context from previous turns:\n" + "\n".join(condensed_points)
    
    # Trim older steps in place, keeping the last 2 steps
    memory.steps = steps[-2:]
    
    return f"✅ Compaction complete. Reduced {total_steps} steps to the latest 2 turns + condensed summary."


class GroupPolicy:
    """Manages allowed Telegram group and topic routing policy with JSON persistence."""
    def __init__(
        self,
        config_path: Path | None = None,
        default_group_id: int | None = None,
        default_topic_id: int | None = None,
        default_topic_name: str = "home",
    ):
        self.config_path = config_path
        self.allowed_group_id: int | None = default_group_id
        self.allowed_topic_id: int | None = default_topic_id
        self.allowed_topic_name: str = default_topic_name
        self.topic_names: dict[int, str] = {}
        self.require_mention_or_reply: bool = True
        self.allow_all_members_in_topic: bool = True

        if self.config_path and self.config_path.exists():
            self.load()

    def load(self):
        if not self.config_path or not self.config_path.exists():
            return
        try:
            content = self.config_path.read_text().strip()
            if not content:
                return
            data = json.loads(content)
            if data.get("allowed_group_id") is not None:
                self.allowed_group_id = int(data["allowed_group_id"])
            if data.get("allowed_topic_id") is not None:
                self.allowed_topic_id = int(data["allowed_topic_id"])
            if data.get("allowed_topic_name"):
                self.allowed_topic_name = str(data["allowed_topic_name"]).strip()
            if "topic_names" in data:
                self.topic_names = {int(k): str(v) for k, v in data["topic_names"].items()}
            if "require_mention_or_reply" in data:
                self.require_mention_or_reply = bool(data["require_mention_or_reply"])
            if "allow_all_members_in_topic" in data:
                self.allow_all_members_in_topic = bool(data["allow_all_members_in_topic"])
        except Exception as e:
            print(f"Warning: Failed to load group policy: {e}")

    def save(self):
        if not self.config_path:
            return
        try:
            data = {
                "allowed_group_id": self.allowed_group_id,
                "allowed_topic_id": self.allowed_topic_id,
                "allowed_topic_name": self.allowed_topic_name,
                "topic_names": {str(k): v for k, v in self.topic_names.items()},
                "require_mention_or_reply": self.require_mention_or_reply,
                "allow_all_members_in_topic": self.allow_all_members_in_topic,
            }
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"Warning: Failed to save group policy: {e}")

    def register_topic(self, thread_id: int | None, name: str):
        if thread_id is not None and name:
            self.topic_names[int(thread_id)] = name.strip()
            if name.strip().lower() == self.allowed_topic_name.lower():
                self.allowed_topic_id = int(thread_id)
            self.save()

    def set_allowed_group(self, group_id: int):
        self.allowed_group_id = int(group_id)
        self.save()

    def set_allowed_topic(self, thread_id: int | None, name: str = "home"):
        if thread_id is not None:
            self.allowed_topic_id = int(thread_id)
            self.allowed_topic_name = name
            self.topic_names[int(thread_id)] = name
            self.save()

    def is_group_allowed(self, chat_id: int) -> bool:
        if self.allowed_group_id is None:
            return True
        return chat_id == self.allowed_group_id

    def is_topic_allowed(self, thread_id: int | None, topic_name: str | None = None) -> bool:
        """
        Validates whether the given topic is the whitelisted 'home' topic.
        """
        # If thread ID explicitly matches allowed_topic_id
        if self.allowed_topic_id is not None:
            return thread_id == self.allowed_topic_id

        # If topic_name matches target name (e.g. 'home')
        if topic_name and topic_name.strip().lower() == self.allowed_topic_name.lower():
            if thread_id is not None:
                self.set_allowed_topic(thread_id, topic_name)
            return True

        # Check registered topic names
        if thread_id is not None and thread_id in self.topic_names:
            return self.topic_names[thread_id].lower() == self.allowed_topic_name.lower()

        # If unconfigured yet, return True so admin can bind
        return True


def should_process_group_message(
    text: str,
    bot_username: str,
    is_reply_to_bot: bool,
    is_command: bool = False,
) -> tuple[bool, str]:
    """
    Determines if a message in a group chat should be processed by the bot.
    Strictly enforces: Only answers if directly replied to this bot or explicitly mentioned.
    Returns (should_process: bool, cleaned_text: str).
    """
    if not text:
        return False, ""

    # 1. Direct reply to this bot's message
    if is_reply_to_bot:
        cleaned = text
        if bot_username:
            pattern = rf'@(?i:{re.escape(bot_username)})\b'
            cleaned = re.sub(pattern, '', cleaned).strip()
        return True, cleaned

    # 2. Command with explicit bot mention (e.g. /status@MeHomyBot)
    first_token = text.split()[0]
    if first_token.startswith("/") and "@" in first_token:
        cmd, target_bot = first_token.split("@", 1)
        if bot_username and target_bot.lower() == bot_username.lower():
            clean_cmd = cmd + text[len(first_token):]
            return True, clean_cmd.strip()
        # Directed to another bot -> strictly ignore
        return False, ""

    # 3. Explicit mention anywhere in text (@MeHomyBot or @MeHomyBot /status)
    if bot_username:
        pattern = rf'@(?i:{re.escape(bot_username)})\b'
        if re.search(pattern, text):
            cleaned = re.sub(pattern, '', text).strip()
            if not cleaned:
                cleaned = "Hello! How can I help you?"
            return True, cleaned

    # Neither mention nor reply to this bot -> strictly silent so user can talk to others uninterrupted
    return False, ""


def get_session_scope_key(chat_id: int, user_id: int, thread_id: int | None = None) -> str:
    """
    Returns an isolated session scope key.
    - Private DM (chat_id > 0): scoped to user_{user_id}
    - Group / Forum (chat_id < 0): scoped to group_{chat_id}_topic_{thread_id or 'general'}
    """
    if chat_id > 0:
        return f"user_{user_id}"
    topic_str = f"t{thread_id}" if thread_id is not None else "general"
    return f"group_{abs(chat_id)}_{topic_str}"


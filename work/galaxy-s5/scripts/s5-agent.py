#!/usr/bin/env python3
"""
S5 Autonomous Edge Agent with Telegram Interface
Powered by Hugging Face smolagents.

Features:
- Multi-turn conversation sessions (new, switch, list, delete)
- Runtime model switching (/model, /models)
- Context compaction (/compact)
- Reasoning visibility toggle (/reasoning on/off)
- Custom system prompt editing (/prompt set/reset)
- File receiving (downloads to /opt/s5-agent/downloads)
- File delivery to Telegram (/get <path> and send_file_to_user tool)
- Telegram command auto-complete registration (set_my_commands)
- Clean Telegram markdown formatting with plain-text fallback
"""

import os
import sys
import time
import asyncio
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Ensure core module can be imported regardless of execution cwd
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from telegram import Update, BotCommand
from telegram.constants import ChatAction, ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from smolagents import CodeAgent, OpenAIServerModel, tool

from s5_agent_core import (
    DEFAULT_SYSTEM_PROMPT,
    BOT_COMMANDS,
    Session,
    UserState,
    GroupPolicy,
    clean_markdown_for_telegram,
    format_agent_response,
    compact_session,
    should_process_group_message,
    get_session_scope_key,
)

# Load environment configuration
ENV_PATH = Path("/opt/s5-agent/.env")
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TEL_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip()
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

# Base directories
DOWNLOADS_DIR = (
    Path("/opt/s5-agent/downloads")
    if Path("/opt/s5-agent").exists()
    else Path("./downloads")
)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Group and topic policy management
GROUP_CONFIG_PATH = (
    Path("/opt/s5-agent/group_config.json")
    if Path("/opt/s5-agent").exists()
    else Path("./group_config.json")
)
env_group_id = os.getenv("TELEGRAM_ALLOWED_GROUP_ID")
env_topic_id = os.getenv("TELEGRAM_ALLOWED_TOPIC_ID")
env_topic_name = os.getenv("TELEGRAM_ALLOWED_TOPIC_NAME") or "home"

group_policy = GroupPolicy(
    config_path=GROUP_CONFIG_PATH,
    default_group_id=int(env_group_id) if env_group_id and env_group_id.lstrip("-").isdigit() else None,
    default_topic_id=int(env_topic_id) if env_topic_id and env_topic_id.isdigit() else None,
    default_topic_name=env_topic_name,
)

BOT_USERNAME: str = "MeHomyBot"
BOT_ID: int = 8818977182

# User/Scope states container
USER_STATES: dict[str, UserState] = {}
PENDING_DELIVERIES: list[str] = []


def get_user_state(scope_key: str) -> UserState:
    if scope_key not in USER_STATES:
        USER_STATES[scope_key] = UserState(user_id=0)
    return USER_STATES[scope_key]


def get_scoped_user_state(update: Update) -> UserState:
    chat = update.effective_chat
    user = update.effective_user
    thread_id = update.effective_message.message_thread_id
    scope_key = get_session_scope_key(chat.id, user.id, thread_id)
    return get_user_state(scope_key)


def is_admin(user_id: int) -> bool:
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True)
    allowed = (os.getenv("TELEGRAM_ALLOWED_USER_ID") or ALLOWED_USER_ID or "").strip()
    if not allowed:
        return True
    return str(user_id) == allowed


def is_authorized_message(update: Update, is_command: bool = False) -> tuple[bool, str]:
    """
    Validates if an update is authorized to be processed according to group, topic, and mention rules.
    Returns (authorized: bool, cleaned_text: str).
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return False, ""

    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    if not is_group:
        # Private DM: must be authorized admin user
        if not is_admin(user.id):
            return False, ""
        return True, msg.text or ""

    # Group message:
    # 0. User filtering: strictly restricted to authorized admin user ID
    if not is_admin(user.id):
        return False, ""

    # 1. Group filtering: must be the allowed group
    if not group_policy.is_group_allowed(chat.id):
        return False, ""

    # 2. Topic filtering: only the topic named 'home'
    thread_id = msg.message_thread_id
    if msg.forum_topic_created:
        group_policy.register_topic(thread_id, msg.forum_topic_created.name)

    if not group_policy.is_topic_allowed(thread_id):
        return False, ""

    # 3. Only answers if explicitly mentioned, replied to, or command directed to this bot
    is_reply_to_bot = False
    if msg.reply_to_message and msg.reply_to_message.from_user:
        # Exclude forum topic root message or topic creation event
        is_topic_root = (
            (thread_id is not None and msg.reply_to_message.message_id == thread_id)
            or bool(msg.reply_to_message.forum_topic_created)
        )
        if not is_topic_root:
            # STRICT: Only replies to THIS bot's exact Telegram ID (never other bots)
            is_reply_to_bot = bool(BOT_ID and msg.reply_to_message.from_user.id == BOT_ID)

    should_run, cleaned = should_process_group_message(
        msg.text or "",
        bot_username=BOT_USERNAME,
        is_reply_to_bot=is_reply_to_bot,
        is_command=is_command,
    )

    print(
        f"[GROUP EVAL] chat={chat.id} thread={thread_id} user={user.id} "
        f"reply_to_bot={is_reply_to_bot} is_cmd={is_command} "
        f"should_run={should_run} text={msg.text[:50]!r}"
    )

    if not should_run:
        return False, ""

    # Lock group and topic if first interaction by admin
    if group_policy.allowed_group_id is None and is_admin(user.id):
        group_policy.set_allowed_group(chat.id)
        print(f"Locked allowed group to {chat.id} ({chat.title})")

    if group_policy.allowed_topic_id is None and thread_id is not None and is_admin(user.id):
        group_policy.set_allowed_topic(thread_id, "home")
        print(f"Locked allowed topic 'home' to thread_id={thread_id}")

    return True, cleaned


# --- Agent Tools ---

@tool
def execute_bash(command: str) -> str:
    """Executes a shell command on the Galaxy S5 Linux device and returns output.
    Args:
        command: The shell command to run (e.g. 'uptime', 's5-wifi status', 'df -h').
    """
    try:
        res = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        out = res.stdout
        if res.stderr:
            out += "\nSTDERR:\n" + res.stderr
        return out.strip() or "(command completed with no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: Command timed out after 45 seconds"
    except Exception as e:
        return f"ERROR: {e}"


@tool
def get_battery_report() -> str:
    """Reads live hardware power supply attributes from sysfs.
    Returns:
        A formatted report with battery capacity, raw SOC, voltage, charger state, and temperature.
    """
    try:
        def rd(p):
            try:
                return Path(p).read_text().strip()
            except Exception:
                return "N/A"

        cap = rd("/sys/class/power_supply/battery/capacity")
        raw_soc = rd("/sys/class/power_supply/battery/batt_read_raw_soc")
        volt = rd("/sys/class/power_supply/battery/voltage_now")
        chg_stat = rd("/sys/class/power_supply/sec-charger/status")
        store_mode = rd("/sys/class/power_supply/battery/store_mode")
        temp = rd("/sys/class/power_supply/battery/temp")

        volt_v = f"{int(volt)/1000000:.3f} V" if volt.isdigit() else volt
        temp_c = f"{int(temp)/10:.1f} °C" if temp.isdigit() else temp

        return (
            f"Battery: {cap}% (Raw SOC: {raw_soc})\n"
            f"Voltage: {volt_v}\n"
            f"Charger State: {chg_stat}\n"
            f"Store Mode (60-70% limit): {'Active' if store_mode == '1' else 'Off'}\n"
            f"Temperature: {temp_c}"
        )
    except Exception as e:
        return f"ERROR reading battery: {e}"


@tool
def manage_wifi(action: str, target: str = "") -> str:
    """Manages Wi-Fi connections on the Galaxy S5 using s5-wifi.
    Args:
        action: 'status' to view current IP/SSID, 'scan' to list available networks, 'list' for saved networks.
        target: Optional network name or ID.
    """
    cmd = f"s5-wifi {action}"
    if target:
        cmd += f" {target}"
    return execute_bash(cmd)


@tool
def send_file_to_user(filepath: str) -> str:
    """Delivers a file from the Galaxy S5 directly to the user's Telegram chat.
    Args:
        filepath: The local file path on the device (e.g. '/var/log/dmesg', '/tmp/photo.jpg').
    """
    p = Path(filepath)
    if not p.exists():
        return f"ERROR: File not found at {filepath}"
    if not p.is_file():
        return f"ERROR: {filepath} is not a regular file"
    PENDING_DELIVERIES.append(str(p.resolve()))
    return f"SUCCESS: Queued file {p.name} ({p.stat().st_size / 1024:.1f} KB) to send to user."


# --- LLM Config & Agent Factory ---

def get_llm_config():
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True)

    openrouter_key = (
        os.getenv("OPENROUTER_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or ""
    ).strip()

    openai_key = (
        os.getenv("OPENAI_API_KEY")
        or os.getenv("LLM_API_KEY")
        or ""
    ).strip()

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    if openrouter_key:
        key = openrouter_key
        default_base = "https://openrouter.ai/api/v1"
        default_model = "openrouter/free"
    elif openai_key:
        key = openai_key
        default_base = "https://api.openai.com/v1"
        default_model = "gpt-4o-mini"
    elif gemini_key:
        key = gemini_key
        default_base = "https://generativelanguage.googleapis.com/v1beta/openai/"
        default_model = "gemini-2.0-flash"
    else:
        key = ""
        default_base = "https://openrouter.ai/api/v1"
        default_model = "openrouter/free"

    base_url = (
        os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or os.getenv("LLM_BASE_URL")
        or default_base
    ).strip()

    model_id = (
        os.getenv("LLM_MODEL_ID")
        or os.getenv("OPENAI_MODEL")
        or os.getenv("MODEL")
        or default_model
    ).strip()

    return key, base_url, model_id


def create_agent_for_session(session: Session, model_override: str | None = None) -> CodeAgent | None:
    key, base_url, default_model = get_llm_config()
    if not key:
        return None

    model_id = model_override or session.model_id or default_model

    client_kwargs = {
        "max_retries": 0,
        "timeout": 25.0,
    }
    if "openrouter.ai" in base_url:
        client_kwargs["default_headers"] = {
            "HTTP-Referer": "https://github.com/galaxy-s5-linux",
            "X-Title": "Galaxy S5 Agent",
        }

    model = OpenAIServerModel(
        model_id=model_id,
        api_base=base_url,
        api_key=key,
        retry=False,
        flatten_messages_as_text=True,
        client_kwargs=client_kwargs,
    )

    agent = CodeAgent(
        tools=[execute_bash, get_battery_report, manage_wifi, send_file_to_user],
        model=model,
        max_steps=6,
        verbosity_level=1,
    )
    return agent


# --- Safe Reply Helper ---

async def send_clean_reply(message, text: str, status_msg=None):
    """Sends reply using Markdown formatting, falling back to plain text if Telegram fails."""
    if len(text) > 4000:
        text = text[:3980] + "\n...[truncated]"

    try:
        if status_msg:
            return await status_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            return await message.reply_markdown(text)
    except Exception:
        # Fallback to plain text if Markdown parsing errors out
        if status_msg:
            return await status_msg.edit_text(text, parse_mode=None)
        else:
            return await message.reply_text(text)


async def send_typing_loop(chat, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


# --- Telegram Command Handlers ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    msg = (
        "🤖 *Galaxy S5 Autonomous Agent*\n\n"
        "I am running natively on your Galaxy S5 postmarketOS node.\n\n"
        "📌 *Essential Commands:*\n"
        "• `/status` — Battery, Wi-Fi & system uptime\n"
        "• `/sessions` — List all conversations\n"
        "• `/session <new|switch|rm>` — Manage sessions\n"
        "• `/group [info|allow]` — Group access configuration\n"
        "• `/topic [info|allow]` — Topic access configuration\n"
        "• `/model [name]` — View or switch LLM model\n"
        "• `/compact` — Compress conversation context\n"
        "• `/reasoning <on|off>` — Toggle thought traces\n"
        "• `/prompt <set|reset>` — Customize system prompt\n"
        "• `/get <path>` — Download file from device\n"
        "• `/reset` — Clear current session memory\n\n"
        "_Tip: In group chats, I will only answer in the 'home' topic when mentioned or replied to._"
    )
    await send_clean_reply(update.message, msg)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.effective_message.message_thread_id
    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
    
    group_allowed = group_policy.is_group_allowed(chat.id) if is_group else True
    topic_allowed = group_policy.is_topic_allowed(thread_id) if is_group else True

    lines = [
        "🆔 *Telegram Context Telemetry*",
        f"• *User ID:* `{user.id}` (@{user.username or 'none'})",
        f"• *Chat ID:* `{chat.id}` ({chat.type}: *{chat.title or 'Private'}*)",
    ]
    if is_group:
        lines.append(f"• *Topic Thread ID:* `{thread_id or 'General'}`")
        lines.append(f"• *Group Whitelist:* `{'Allowed' if group_allowed else 'Rejected'}`")
        lines.append(f"• *Topic Whitelist:* `{'Allowed' if topic_allowed else 'Rejected'}`")
        if not group_policy.allowed_group_id and is_admin(user.id):
            lines.append("\n💡 _Tip: Run `/group allow` to lock the bot to this group._")
        if not group_policy.allowed_topic_id and is_admin(user.id):
            lines.append("💡 _Tip: Run `/topic allow home` in topic 'home' to lock routing._")
    
    await send_clean_reply(update.message, "\n".join(lines))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    report = get_battery_report()
    wifi = manage_wifi("status")
    uptime = execute_bash("uptime")

    msg = f"📊 *System Telemetry*\n\n{report}\n\n*Wi-Fi:*\n{wifi}\n\n*Uptime:*\n`{uptime}`"
    await send_clean_reply(update.message, msg)


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    state = get_scoped_user_state(update)
    items = state.list_sessions()

    lines = ["📋 *Conversation Sessions:*"]
    for s in items:
        badge = " *(active)*" if s["is_active"] else ""
        mins = int((time.time() - s["last_active"]) / 60)
        lines.append(
            f"• `{s['session_id']}` (*{s['name']}*){badge} — {s['message_count']} msgs | `{s['model_id'] or 'default'}` | {mins}m ago"
        )

    lines.append("\n_Commands:_")
    lines.append("• `/session new [name]` — Create new session")
    lines.append("• `/session switch <id>` — Switch to session")
    lines.append("• `/session rm <id>` — Delete session")
    await send_clean_reply(update.message, "\n".join(lines))


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    state = get_scoped_user_state(update)
    if not context.args or context.args[0].lower() in ["list", "ls"]:
        await cmd_sessions(update, context)
        return

    sub = context.args[0].lower()
    if sub in ["new", "create"]:
        name = context.args[1] if len(context.args) > 1 else ""
        sid = state.create_session(name=name)
        await send_clean_reply(
            update.message,
            f"✨ Created & switched to new session: `{sid}` (*{state.sessions[sid].name}*)",
        )
    elif sub in ["switch", "resume", "load"]:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/session switch <id_or_name>`")
            return
        target = context.args[1]
        ok_sw, msg = state.switch_session(target)
        await send_clean_reply(update.message, msg)
    elif sub in ["rm", "delete", "remove"]:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/session rm <id_or_name>`")
            return
        target = context.args[1]
        ok_rm, msg = state.remove_session(target)
        await send_clean_reply(update.message, msg)
    else:
        await update.message.reply_text(
            "Usage:\n• `/session new [name]`\n• `/session switch <id>`\n• `/session rm <id>`\n• `/session list`"
        )


async def cmd_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    chat = update.effective_chat
    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    if not context.args:
        cur_gid = group_policy.allowed_group_id
        is_cur = (chat.id == cur_gid) if cur_gid else False
        msg = (
            f"👥 *Telegram Group Configuration*\n\n"
            f"• *Current Chat ID:* `{chat.id}` ({chat.title or 'Private'})\n"
            f"• *Allowed Group ID:* `{cur_gid or '(none locked yet)'}`\n"
            f"• *Locked to this group:* `{'Yes' if is_cur else 'No'}`\n"
            f"• *Allowed Topic ID:* `{group_policy.allowed_topic_id or '(none)'}` (*{group_policy.allowed_topic_name}*)\n\n"
            f"_Commands:_\n"
            f"• `/group allow` — Lock bot to this group\n"
            f"• `/group set <chat_id>` — Set group ID explicitly\n"
            f"• `/group reset` — Clear group restriction"
        )
        await send_clean_reply(update.message, msg)
        return

    sub = context.args[0].lower()
    if sub in ["allow", "bind", "lock"]:
        if not is_group:
            await send_clean_reply(update.message, "❌ `/group allow` must be run inside the target Telegram group.")
            return
        group_policy.set_allowed_group(chat.id)
        await send_clean_reply(
            update.message,
            f"✅ *Group Locked!*\nBot is now restricted exclusively to this group (`{chat.id}`: *{chat.title}*).",
        )
    elif sub == "set":
        if len(context.args) < 2:
            await send_clean_reply(update.message, "Usage: `/group set <group_chat_id>`")
            return
        try:
            gid = int(context.args[1])
            group_policy.set_allowed_group(gid)
            await send_clean_reply(update.message, f"✅ Allowed group ID set to `{gid}`.")
        except ValueError:
            await send_clean_reply(update.message, "❌ Invalid group ID. Must be an integer (e.g. `-100...`).")
    elif sub in ["reset", "clear", "rm"]:
        group_policy.allowed_group_id = None
        group_policy.save()
        await send_clean_reply(update.message, "🔓 Group restriction cleared.")
    else:
        await send_clean_reply(update.message, "Usage: `/group [allow|set <id>|reset|info]`")


async def cmd_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    thread_id = update.effective_message.message_thread_id

    if not context.args or context.args[0].lower() in ["info", "list"]:
        tid = group_policy.allowed_topic_id
        tname = group_policy.allowed_topic_name
        is_cur = (thread_id == tid) if tid is not None else False
        msg = (
            f"🏷️ *Forum Topic Configuration*\n\n"
            f"• *Current Thread ID:* `{thread_id or 'General / None'}`\n"
            f"• *Allowed Topic Name:* `{tname}`\n"
            f"• *Allowed Thread ID:* `{tid or '(not locked yet)'}`\n"
            f"• *Current topic matches whitelist:* `{'Yes' if is_cur else 'No'}`\n\n"
            f"_Commands:_\n"
            f"• `/topic allow [name]` — Lock whitelist to this topic (default 'home')\n"
            f"• `/topic set <id> [name]` — Set allowed topic thread ID\n"
            f"• `/topic reset` — Clear topic restriction"
        )
        await send_clean_reply(update.message, msg)
        return

    sub = context.args[0].lower()
    if sub in ["allow", "bind", "lock", "home"]:
        name = context.args[1] if len(context.args) > 1 else "home"
        group_policy.set_allowed_topic(thread_id, name=name)
        await send_clean_reply(
            update.message,
            f"✅ *Topic Locked!*\nBot will now only answer in topic *{name}* (`thread_id: {thread_id}`) when mentioned or replied to.",
        )
    elif sub == "set":
        if len(context.args) < 2:
            await send_clean_reply(update.message, "Usage: `/topic set <thread_id> [name]`")
            return
        try:
            tid = int(context.args[1])
            name = context.args[2] if len(context.args) > 2 else "home"
            group_policy.set_allowed_topic(tid, name=name)
            await send_clean_reply(update.message, f"✅ Allowed topic set to `{tid}` (*{name}*).")
        except ValueError:
            await send_clean_reply(update.message, "❌ Invalid thread ID. Must be an integer.")
    elif sub in ["reset", "clear", "rm"]:
        group_policy.allowed_topic_id = None
        group_policy.save()
        await send_clean_reply(update.message, "🔓 Topic restriction cleared.")
    else:
        await send_clean_reply(update.message, "Usage: `/topic [allow|set <id>|reset|info]`")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    state = get_scoped_user_state(update)
    session = state.get_active_session()
    session.agent = None
    session.message_count = 0
    await send_clean_reply(
        update.message,
        f"🧹 Memory cleared for session `{session.session_id}` (*{session.name}*).",
    )


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    state = get_scoped_user_state(update)
    session = state.get_active_session()
    msg = compact_session(session)
    await send_clean_reply(update.message, msg)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return
    if not is_admin(update.effective_user.id):
        await send_clean_reply(update.message, "❌ Only the device admin can change LLM models.")
        return

    state = get_scoped_user_state(update)
    session = state.get_active_session()
    key, base_url, default_model = get_llm_config()
    current_model = session.model_id or default_model

    if not context.args:
        msg = (
            f"🤖 *Active Model:* `{current_model}`\n"
            f"🌐 *Provider Base URL:* `{base_url}`\n\n"
            f"To switch model for session *{session.name}*:\n"
            f"`/model <model_id>`\n\n"
            f"_Type `/models` to view tested free options._"
        )
        await send_clean_reply(update.message, msg)
        return

    new_model = context.args[0].strip()
    session.model_id = new_model
    session.agent = None  # Rebuild with new model on next turn
    await send_clean_reply(
        update.message,
        f"✅ Model switched to: `{new_model}` for session *{session.name}*.",
    )


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    msg = (
        "🌟 *Recommended Free OpenRouter Models:*\n\n"
        "• `openrouter/free` (⭐ Auto-balanced across healthy free providers)\n"
        "• `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` (1.5s latency, 256k ctx)\n"
        "• `dots-studio/dots-3-note-preview:free` (512k ctx)\n"
        "• `inclusionai/ling-3.0-flash-sante:free` (262k ctx)\n"
        "• `liquid/lfm-2.5-2.6b:free` (Fast & lightweight)\n"
        "• `cohere/north-mini-code:free` (Code specialist)\n"
        "• `nvidia/nemotron-3.5-lightning:free` (1M ctx)\n\n"
        "_Switch with:_ `/model <model_id>`"
    )
    await send_clean_reply(update.message, msg)


async def cmd_reasoning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return

    state = get_scoped_user_state(update)
    session = state.get_active_session()

    if not context.args:
        status = "ON" if session.show_reasoning else "OFF"
        msg = (
            f"🧠 *Reasoning Visibility:* `{status}`\n\n"
            f"• `/reasoning on` — Display thoughts and tool execution steps\n"
            f"• `/reasoning off` — Display clean final answers only"
        )
        await send_clean_reply(update.message, msg)
        return

    arg = context.args[0].lower().strip()
    if arg in ["on", "true", "1", "yes"]:
        session.show_reasoning = True
        await send_clean_reply(
            update.message,
            "🧠 Reasoning mode: *ON*. Intermediate thoughts and tool steps will be shown.",
        )
    elif arg in ["off", "false", "0", "no"]:
        session.show_reasoning = False
        await send_clean_reply(
            update.message,
            "🧠 Reasoning mode: *OFF*. Showing clean final answers only.",
        )
    else:
        await update.message.reply_text("Usage: `/reasoning on` or `/reasoning off`")


async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return
    if not is_admin(update.effective_user.id):
        await send_clean_reply(update.message, "❌ Only the device admin can customize system prompts.")
        return

    state = get_scoped_user_state(update)
    session = state.get_active_session()

    if not context.args:
        msg = (
            f"📝 *System Prompt for session* `{session.name}`:\n\n"
            f"```\n{session.system_prompt}\n```\n\n"
            f"• `/prompt set <text>` — Update prompt\n"
            f"• `/prompt reset` — Revert to default"
        )
        await send_clean_reply(update.message, msg)
        return

    sub = context.args[0].lower()
    if sub == "reset":
        session.system_prompt = DEFAULT_SYSTEM_PROMPT
        session.agent = None
        await send_clean_reply(update.message, "✅ System prompt reset to default.")
    elif sub == "set":
        new_prompt = " ".join(context.args[1:]).strip()
        if not new_prompt:
            await update.message.reply_text("Usage: `/prompt set <your instructions>`")
            return
        session.system_prompt = new_prompt
        session.agent = None
        await send_clean_reply(
            update.message,
            f"✅ System prompt updated for *{session.name}*:\n`{new_prompt[:120]}...`",
        )
    else:
        await update.message.reply_text("Usage: `/prompt set <text>` or `/prompt reset`")


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=True)
    if not ok:
        return
    if not is_admin(update.effective_user.id):
        await send_clean_reply(update.message, "❌ Only the device admin can download system files.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/get <filepath>`\nExample: `/get /var/log/s5-agent.log`")
        return

    raw_path = " ".join(context.args).strip()
    target = Path(raw_path)
    if not target.exists():
        await send_clean_reply(update.message, f"❌ File not found: `{raw_path}`")
        return

    if not target.is_file():
        await send_clean_reply(update.message, f"❌ Path is not a regular file: `{raw_path}`")
        return

    size_mb = target.stat().st_size / (1024 * 1024)
    if size_mb > 50:
        await send_clean_reply(update.message, f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")
        return

    await update.message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)
    with open(target, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=target.name,
            caption=f"📄 `{target.name}` ({size_mb:.2f} MB)",
            parse_mode=ParseMode.MARKDOWN,
        )


# --- File Upload Handlers ---

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=False)
    if not ok:
        return

    doc = update.message.document
    if not doc:
        return

    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', doc.file_name or "file")
    target_path = DOWNLOADS_DIR / safe_name

    file_obj = await context.bot.get_file(doc.file_id)
    await file_obj.download_to_drive(custom_path=target_path)

    size_kb = target_path.stat().st_size / 1024
    msg = (
        f"📥 *File Received & Saved!*\n"
        f"• *Name:* `{target_path.name}`\n"
        f"• *Path:* `{target_path.resolve()}`\n"
        f"• *Size:* `{size_kb:.1f} KB`\n\n"
        f"_You can now ask the agent to inspect or process this file._"
    )
    await send_clean_reply(update.message, msg)

    # Inform active session
    state = get_scoped_user_state(update)
    session = state.get_active_session()
    if session.agent:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: session.agent.run(
                    f"System Event: User uploaded file to `{target_path.resolve()}` ({size_kb:.1f} KB).",
                    reset=False,
                ),
            )
        except Exception:
            pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, _ = is_authorized_message(update, is_command=False)
    if not ok:
        return

    photos = update.message.photo
    if not photos:
        return

    best_photo = photos[-1]
    target_path = DOWNLOADS_DIR / f"photo_{int(time.time())}.jpg"

    file_obj = await context.bot.get_file(best_photo.file_id)
    await file_obj.download_to_drive(custom_path=target_path)

    size_kb = target_path.stat().st_size / 1024
    msg = (
        f"📷 *Photo Saved!*\n"
        f"• *Path:* `{target_path.resolve()}`\n"
        f"• *Size:* `{size_kb:.1f} KB`"
    )
    await send_clean_reply(update.message, msg)


# --- Natural Language Message Handler ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg or not msg.text:
        return

    ok, clean_text = is_authorized_message(update, is_command=False)
    if not ok:
        return

    text = clean_text.strip()

    # Clean leading mention or @bot syntax
    clean_cmd_text = re.sub(rf'@(?i:{re.escape(BOT_USERNAME)})\b', '', text).strip()
    if clean_cmd_text and "@" in clean_cmd_text.split()[0]:
        cmd_p, tgt = clean_cmd_text.split()[0].split("@", 1)
        if tgt.lower() == BOT_USERNAME.lower():
            clean_cmd_text = cmd_p + clean_cmd_text[len(clean_cmd_text.split()[0]):]
    clean_cmd_text = clean_cmd_text.strip()

    first_word = clean_cmd_text.split()[0].lstrip("/").lower() if clean_cmd_text else ""

    # Instant fast-path telemetry commands (zero LLM / zero latency)
    if first_word == "status":
        report = get_battery_report()
        wifi = manage_wifi("status")
        uptime = execute_bash("uptime")
        msg = f"📊 *System Telemetry*\n\n{report}\n\n*Wi-Fi:*\n{wifi}\n\n*Uptime:*\n`{uptime}`"
        return await send_clean_reply(update.message, msg)

    if first_word == "id":
        thread_id = update.effective_message.message_thread_id
        msg = (
            f"🆔 *Telegram Context*\n\n"
            f"• *User ID:* `{user.id}` ({user.full_name})\n"
            f"• *Chat ID:* `{chat.id}` ({chat.type})\n"
            f"• *Topic/Thread ID:* `{thread_id or 'General (None)'}`\n"
            f"• *Admin:* {'Yes' if is_admin(user.id) else 'No'}\n"
            f"• *Active Session:* `{get_scoped_user_state(update).active_session_id}`"
        )
        return await send_clean_reply(update.message, msg)

    if first_word == "ping":
        return await update.message.reply_text("🏓 Pong from Galaxy S5 Linux!")

    # Route leading commands if user mentioned bot before command (e.g. "@MeHomyBot /model" or "@MeHomyBot sessions")
    command_map = {
        "sessions": cmd_sessions,
        "session": cmd_session,
        "compact": cmd_compact,
        "reset": cmd_reset,
        "new": cmd_reset,
        "model": cmd_model,
        "models": cmd_models,
        "reasoning": cmd_reasoning,
        "prompt": cmd_prompt,
        "get": cmd_get,
        "group": cmd_group,
        "topic": cmd_topic,
        "help": cmd_start,
        "start": cmd_start,
    }
    if first_word in command_map:
        context.args = clean_cmd_text.split()[1:]
        return await command_map[first_word](update, context)

    state = get_scoped_user_state(update)
    session = state.get_active_session()

    try:
        if session.agent is None:
            session.agent = create_agent_for_session(session)
    except Exception as e:
        await update.message.reply_text(f"❌ Configuration error initializing agent: {e}")
        return

    if session.agent is None:
        await update.message.reply_text(
            "⚠️ LLM API Key is missing. Please set OPENROUTER_KEY, OPENAI_API_KEY, or GEMINI_API_KEY in /opt/s5-agent/.env"
        )
        return

    status_msg = await update.message.reply_text("⚡ Thinking and working...")

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(send_typing_loop(update.effective_chat, stop_event))

    loop = asyncio.get_running_loop()
    PENDING_DELIVERIES.clear()

    try:
        try:
            # reset=False keeps multi-turn conversation memory within this session
            result = await loop.run_in_executor(None, lambda: session.agent.run(text, reset=False))
        except Exception as run_err:
            err_str = str(run_err)
            if "free-models-per-day" in err_str.lower():
                await send_clean_reply(
                    update.message,
                    "⚠️ *OpenRouter Daily Free Limit Reached* (50/50 requests used today).\n\n"
                    "Options to resume:\n"
                    "1. Add $5 credit to OpenRouter (unlocks 1,000 free model requests/day).\n"
                    "2. Add `GEMINI_API_KEY=...` to `.env` (Google AI Studio gives 1,500 requests/day free).\n"
                    "3. Wait for the OpenRouter daily reset at midnight UTC.",
                    status_msg=status_msg,
                )
                return

            key, base_url, default_model = get_llm_config()
            curr_model = session.model_id or default_model
            is_upstream_err = any(x in err_str.lower() for x in [
                "429", "rate-limit", "nonetype", "502", "503", "500",
                "unavailable", "exhausted", "error in generating model output"
            ])
            if is_upstream_err and "openrouter.ai" in base_url and curr_model != "openrouter/free":
                await send_clean_reply(
                    update.message,
                    f"⚠️ `{curr_model}` experienced an upstream error. Auto-switching to `openrouter/free`...",
                    status_msg=status_msg,
                )
                session.model_id = "openrouter/free"
                fallback_agent = create_agent_for_session(session, model_override="openrouter/free")
                result = await loop.run_in_executor(None, lambda: fallback_agent.run(text, reset=False))
                session.agent = fallback_agent
            elif "context" in err_str.lower() or "maximum context length" in err_str.lower():
                await send_clean_reply(
                    update.message,
                    "⚠️ Context limit reached. Auto-compacting session memory and continuing...",
                    status_msg=status_msg,
                )
                compact_session(session)
                result = await loop.run_in_executor(None, lambda: session.agent.run(text, reset=False))
            else:
                raise run_err

        stop_event.set()
        await typing_task

        session.touch()

        # Format output cleanly
        formatted_output = format_agent_response(
            result,
            thoughts=[],
            show_reasoning=session.show_reasoning,
        )
        await send_clean_reply(update.message, formatted_output, status_msg=status_msg)

        # Deliver any outbound files requested by the agent tool
        if PENDING_DELIVERIES:
            for filepath in list(PENDING_DELIVERIES):
                p = Path(filepath)
                if p.exists() and p.is_file():
                    with open(p, "rb") as f:
                        await update.message.reply_document(
                            document=f,
                            filename=p.name,
                            caption=f"📁 Delivered: `{p.name}`",
                            parse_mode=ParseMode.MARKDOWN,
                        )
            PENDING_DELIVERIES.clear()

    except Exception as e:
        stop_event.set()
        await typing_task
        await send_clean_reply(update.message, f"❌ Error during execution: {e}", status_msg=status_msg)


# --- Startup & Auto-Complete Registration ---

async def post_init(application: Application):
    """Registers bot commands with Telegram so clients auto-complete them and resolves bot identity."""
    global BOT_USERNAME, BOT_ID
    try:
        me = await application.bot.get_me()
        BOT_USERNAME = me.username or "MeHomyBot"
        BOT_ID = me.id
        print(f"Bot connected as @{BOT_USERNAME} (ID: {BOT_ID})")
        await application.bot.set_my_commands(BOT_COMMANDS)
        print("Telegram bot commands registered for auto-complete successfully.")
    except Exception as e:
        print(f"Warning: Failed to set bot commands or resolve identity: {e}", file=sys.stderr)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set in environment or /opt/s5-agent/.env", file=sys.stderr)
        sys.exit(1)

    print("Starting S5 Telegram Agent with Full Feature Suite & Group/Topic Routing...")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Core commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))

    # Access control & routing commands
    app.add_handler(CommandHandler("group", cmd_group))
    app.add_handler(CommandHandler("topic", cmd_topic))

    # Session commands
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("new", cmd_reset))
    app.add_handler(CommandHandler("compact", cmd_compact))

    # Configuration commands
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("reasoning", cmd_reasoning))
    app.add_handler(CommandHandler("prompt", cmd_prompt))

    # File commands
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Natural language conversation
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()


if __name__ == "__main__":
    main()

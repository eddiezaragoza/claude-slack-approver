#!/usr/bin/env python3
"""
Claude Code PreToolUse hook: Slack two-way approval with smart filtering.

Posts a Slack message and polls for emoji reactions (thumbs up/down) or
thread replies with text feedback. Safe commands are auto-approved without
posting to Slack.

Exit codes:
  0 — allow (or timeout/fallback to terminal prompt)
  2 — deny
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
RULES_FILE = os.path.join(SCRIPT_DIR, "command-rules.json")
PENDING_FILE = "/tmp/claude-slack-pending.json"

POLL_INTERVAL = 2        # seconds between polls
TIMEOUT = 300            # 5 minutes total

APPROVE_REACTIONS = {"+1", "thumbsup"}
DENY_REACTIONS = {"-1", "thumbsdown"}

APPROVE_KEYWORDS = {"ok", "okay", "approve", "approved", "lgtm", "yes", "go ahead", "go", "yep", "yup", "sure", "do it", "proceed", "ship it", "y"}

FOOTER_TEXT = ":thumbsup: = approve | :thumbsdown: = deny | _Reply in thread with feedback_"


def load_env(path):
    """Load KEY=VALUE pairs from a file into a dict."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


config = load_env(ENV_FILE)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", config.get("SLACK_BOT_TOKEN", ""))
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", config.get("SLACK_CHANNEL_ID", ""))

# ---------------------------------------------------------------------------
# Command classification (Phase 1)
# ---------------------------------------------------------------------------

def load_command_rules():
    """Load command classification rules from JSON file."""
    if not os.path.isfile(RULES_FILE):
        return None
    try:
        with open(RULES_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return None


def _split_chained_commands(cmd):
    """Split a command string on &&, ||, ;, and | into individual segments."""
    # Split on shell operators, keeping it simple (doesn't handle quoted strings
    # with these chars, but good enough for classification)
    segments = re.split(r'\s*(?:&&|\|\||[;|])\s*', cmd)
    return [s.strip() for s in segments if s.strip()]


def classify_command(cmd):
    """
    Classify a shell command as 'safe', 'risky', or 'deny'.

    For piped/chained commands, each segment is classified individually.
    If any segment is risky or denied, the whole command gets that classification
    (deny takes priority over risky).
    """
    rules = load_command_rules()
    if rules is None:
        return "risky"  # fail-safe: require approval if rules can't load

    segments = _split_chained_commands(cmd)
    worst = "safe"

    for segment in segments:
        rating = _classify_single(segment, rules)
        if rating == "deny":
            return "deny"
        if rating == "risky":
            worst = "risky"

    return worst


def _classify_single(cmd, rules):
    """Classify a single (non-chained) command segment."""
    cmd_stripped = cmd.strip()

    # Check deny patterns first
    for pattern in rules.get("deny", {}).get("patterns", []):
        if re.search(pattern, cmd_stripped):
            return "deny"

    # Check risky prefixes
    for prefix in rules.get("risky", {}).get("prefixes", []):
        if cmd_stripped == prefix or cmd_stripped.startswith(prefix + " "):
            return "risky"

    # Check risky patterns
    for pattern in rules.get("risky", {}).get("patterns", []):
        if re.search(pattern, cmd_stripped):
            return "risky"

    # Check safe prefixes
    for prefix in rules.get("safe", {}).get("prefixes", []):
        if cmd_stripped == prefix or cmd_stripped.startswith(prefix + " "):
            return "safe"

    # Default: risky (require approval for unknown commands)
    return "risky"


# ---------------------------------------------------------------------------
# Slack helpers (stdlib only)
# ---------------------------------------------------------------------------

def slack_post(method, payload):
    """POST to a Slack Web API method. Returns parsed JSON response."""
    url = f"https://slack.com/api/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def slack_get(method, params):
    """GET a Slack Web API method with query params. Returns parsed JSON."""
    qs = urllib.parse.urlencode(params)
    url = f"https://slack.com/api/{method}?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def get_bot_user_id():
    """Get the bot's own user ID so we can filter out our own thread replies."""
    resp = slack_post("auth.test", {})
    if resp.get("ok"):
        return resp.get("user_id")
    return None


def post_message(text, thread_ts=None):
    """Post a message to the configured Slack channel. Returns message ts."""
    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = slack_post("chat.postMessage", payload)
    if resp.get("ok"):
        return resp["ts"]
    print(f"Slack post failed: {resp.get('error')}", file=sys.stderr)
    return None


def update_message(ts, text):
    """Update an existing Slack message."""
    slack_post("chat.update", {
        "channel": SLACK_CHANNEL_ID,
        "ts": ts,
        "text": text,
    })


def check_reactions(msg_ts):
    """
    Check emoji reactions on a message.
    Returns "allow", "deny", or None.
    """
    resp = slack_get("reactions.get", {
        "channel": SLACK_CHANNEL_ID,
        "timestamp": msg_ts,
        "full": "true",
    })
    if not resp.get("ok"):
        return None

    message = resp.get("message", {})
    reactions = message.get("reactions", [])

    for reaction in reactions:
        # Reaction names: "+1", "+1::skin-tone-2", "thumbsup", etc.
        name = reaction.get("name", "").split("::")[0]
        if name in APPROVE_REACTIONS:
            return "allow"
        if name in DENY_REACTIONS:
            return "deny"
    return None


def check_thread_replies(msg_ts, bot_user_id):
    """
    Check thread replies on a message for text-based approval/denial.

    Returns a tuple: (decision, feedback_text)
      - ("allow", "feedback...") if reply starts with approval keyword
      - ("deny", "feedback...") if reply is anything else
      - (None, None) if no human replies yet
    """
    resp = slack_get("conversations.replies", {
        "channel": SLACK_CHANNEL_ID,
        "ts": msg_ts,
        "limit": "20",
    })
    if not resp.get("ok"):
        return None, None

    messages = resp.get("messages", [])

    # Skip the first message (the original approval request)
    for msg in messages[1:]:
        user = msg.get("user", "")
        # Skip bot's own replies
        if bot_user_id and user == bot_user_id:
            continue
        # Skip bot messages (subtype check)
        if msg.get("subtype") == "bot_message":
            continue

        text = msg.get("text", "").strip()
        if not text:
            continue

        # Check if the reply is an approval
        text_lower = text.lower().strip()
        for keyword in APPROVE_KEYWORDS:
            if text_lower == keyword or text_lower.startswith(keyword + " ") or text_lower.startswith(keyword + ","):
                return "allow", text

        # Any other text = deny with feedback
        return "deny", text

    return None, None


# ---------------------------------------------------------------------------
# Pending file — lets PostToolUse hook update Slack after terminal approval
# ---------------------------------------------------------------------------

def save_pending(msg_ts, original_text):
    """Save pending approval info so PostToolUse can update Slack."""
    with open(PENDING_FILE, "w") as f:
        json.dump({
            "msg_ts": msg_ts,
            "channel": SLACK_CHANNEL_ID,
            "original_text": original_text,
            "timestamp": time.time(),
        }, f)


def clear_pending():
    """Remove the pending file."""
    try:
        os.unlink(PENDING_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Format the approval message
# ---------------------------------------------------------------------------

def get_project_context(tool_input):
    """Derive project name from file paths or cwd."""
    for key in ("file_path", "path"):
        p = tool_input.get(key, "")
        if p:
            parts = p.replace("\\", "/").split("/")
            if "projects" in parts:
                idx = parts.index("projects")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    cwd = os.getcwd()
    return os.path.basename(cwd)


def format_tool_request(hook_input):
    """Build a human-readable Slack message for the tool request."""
    tool_name = hook_input.get("tool_name", "Unknown")
    tool_input = hook_input.get("tool_input", {})
    session_id = hook_input.get("session_id", "unknown")

    project = get_project_context(tool_input)
    description = tool_input.get("description", "")

    lines = [":lock: *Claude Code — Approval Needed*"]
    lines.append(f"*Project:* `{project}` | *Session:* `{session_id[:8]}`")

    if description:
        lines.append(f"*Why:* {description}")

    lines.append(f"*Tool:* `{tool_name}`")

    if tool_name == "Bash" and tool_input.get("command"):
        cmd = tool_input["command"]
        if len(cmd) > 1500:
            cmd = cmd[:1500] + "\n... (truncated)"
        lines.append(f"```\n{cmd}\n```")
    elif tool_name == "Write" and tool_input.get("file_path"):
        lines.append(f"*File:* `{tool_input['file_path']}`")
        content = tool_input.get("content", "")
        if content:
            preview = content[:500]
            if len(content) > 500:
                preview += "\n... (truncated)"
            lines.append(f"```\n{preview}\n```")
    elif tool_name == "Edit" and tool_input.get("file_path"):
        lines.append(f"*File:* `{tool_input['file_path']}`")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        if old:
            lines.append(f"Replace:\n```\n{old[:300]}\n```")
        if new:
            lines.append(f"With:\n```\n{new[:300]}\n```")
    else:
        summary = json.dumps(tool_input, indent=2)
        if len(summary) > 1000:
            summary = summary[:1000] + "\n..."
        lines.append(f"```\n{summary}\n```")

    lines.append(f"\n{FOOTER_TEXT}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resolve pending — called by PostToolUse hook
# ---------------------------------------------------------------------------

def resolve_pending():
    """Called after a tool completes. If there's a pending Slack message,
    update it to show it was approved via the terminal."""
    if not os.path.isfile(PENDING_FILE):
        return
    try:
        with open(PENDING_FILE) as f:
            pending = json.load(f)
    except (json.JSONDecodeError, ValueError):
        clear_pending()
        return

    msg_ts = pending.get("msg_ts")
    original_text = pending.get("original_text", "")

    if msg_ts and SLACK_BOT_TOKEN:
        updated = original_text.replace(
            FOOTER_TEXT,
            ":white_check_mark: *Approved via terminal*"
        )
        update_message(msg_ts, updated)

    clear_pending()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Check if called as post-tool resolver
    if len(sys.argv) > 1 and sys.argv[1] == "--resolve":
        resolve_pending()
        return

    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "Unknown")
    tool_input = hook_input.get("tool_input", {})

    # Validate config
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        print("Missing SLACK_BOT_TOKEN or SLACK_CHANNEL_ID", file=sys.stderr)
        sys.exit(0)

    # --- Phase 1: Smart command filtering for Bash commands ---
    if tool_name == "Bash" and tool_input.get("command"):
        classification = classify_command(tool_input["command"])

        if classification == "safe":
            # Auto-approve without posting to Slack
            result = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Auto-approved: safe command",
            }}
            print(json.dumps(result))
            sys.exit(0)

        elif classification == "deny":
            # Block immediately without posting to Slack
            result = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Blocked: command matches deny rules",
            }}
            print(json.dumps(result))
            sys.exit(0)

        # classification == "risky" — fall through to Slack approval

    # --- Post the approval request to Slack ---
    # If SLACK_THREAD_TS is set (by the daemon), post in that thread
    thread_ts = os.environ.get("SLACK_THREAD_TS")
    message_text = format_tool_request(hook_input)

    if thread_ts:
        msg_ts = post_message(message_text, thread_ts=thread_ts)
    else:
        msg_ts = post_message(message_text)

    if not msg_ts:
        sys.exit(0)

    # Save pending file so PostToolUse can update if terminal approves
    save_pending(msg_ts, message_text)

    # Get bot user ID for filtering thread replies
    bot_user_id = get_bot_user_id()

    # --- Phase 2: Poll for emoji reactions AND thread replies ---
    start = time.time()
    while time.time() - start < TIMEOUT:
        time.sleep(POLL_INTERVAL)

        # Check emoji reactions first
        decision = check_reactions(msg_ts)

        if decision == "allow":
            updated = message_text.replace(
                FOOTER_TEXT,
                ":white_check_mark: *Approved via Slack*"
            )
            update_message(msg_ts, updated)
            clear_pending()
            result = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Approved via Slack",
            }}
            print(json.dumps(result))
            sys.exit(0)

        elif decision == "deny":
            updated = message_text.replace(
                FOOTER_TEXT,
                ":no_entry_sign: *Denied via Slack*"
            )
            update_message(msg_ts, updated)
            clear_pending()
            result = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Denied via Slack",
            }}
            print(json.dumps(result))
            sys.exit(0)

        # Check thread replies for text-based feedback
        reply_decision, reply_text = check_thread_replies(msg_ts, bot_user_id)

        if reply_decision == "allow":
            reason = f"Approved via Slack thread"
            if reply_text:
                reason = f"Approved via Slack thread. User feedback: {reply_text}"
            updated = message_text.replace(
                FOOTER_TEXT,
                f":white_check_mark: *Approved via thread reply*"
            )
            update_message(msg_ts, updated)
            clear_pending()
            result = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": reason,
            }}
            print(json.dumps(result))
            sys.exit(0)

        elif reply_decision == "deny":
            reason = f"User feedback: {reply_text}"
            updated = message_text.replace(
                FOOTER_TEXT,
                f":speech_balloon: *Denied with feedback:* {reply_text}"
            )
            update_message(msg_ts, updated)
            clear_pending()
            result = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}
            print(json.dumps(result))
            sys.exit(0)

    # Timeout — fall back to terminal prompt (keep pending file for PostToolUse)
    updated = message_text.replace(
        FOOTER_TEXT,
        ":hourglass: *Waiting for terminal approval...*"
    )
    update_message(msg_ts, updated)
    sys.exit(0)


if __name__ == "__main__":
    main()

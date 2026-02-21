#!/usr/bin/env python3
"""
Claude Code Slack Chat Daemon.

Long-running process that monitors a Slack channel for messages, bridges them
to Claude Code CLI sessions, and posts responses back as thread replies.

Features:
  - New top-level message → starts a new Claude session
  - Thread reply → continues existing session via --resume
  - Project directory resolved from message prefix (e.g., "calabrio: prompt")
  - Concurrent sessions via threading
  - Sets SLACK_THREAD_TS env var so the PreToolUse hook posts approvals
    in the same conversation thread

Usage:
  python3 claude-slack-daemon.py
  # or via tmux:
  tmux new-session -d -s claude-daemon "python3 /path/to/claude-slack-daemon.py"
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
SESSIONS_FILE = os.path.join(SCRIPT_DIR, "sessions.json")

SESSION_MAX_AGE = 86400  # 24 hours


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
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHAT_CHANNEL_ID", config.get("SLACK_CHAT_CHANNEL_ID", ""))
DEFAULT_PROJECT_DIR = config.get("DEFAULT_PROJECT_DIR", "/mnt/c/projects")
POLL_INTERVAL = int(config.get("DAEMON_POLL_INTERVAL", "3"))

# Build project shorthand map from PROJECT_* keys in .env
PROJECT_MAP = {}
for key, value in config.items():
    if key.startswith("PROJECT_"):
        shorthand = key[len("PROJECT_"):].lower()
        PROJECT_MAP[shorthand] = value


# ---------------------------------------------------------------------------
# Slack helpers
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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def get_bot_user_id():
    """Get the bot's own user ID."""
    resp = slack_post("auth.test", {})
    if resp.get("ok"):
        return resp.get("user_id")
    return None


def post_message(text, thread_ts=None):
    """Post a message to the channel. Returns message ts."""
    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = slack_post("chat.postMessage", payload)
    if resp.get("ok"):
        return resp["ts"]
    return None


def update_message(ts, text):
    """Update an existing Slack message."""
    slack_post("chat.update", {
        "channel": SLACK_CHANNEL_ID,
        "ts": ts,
        "text": text,
    })


# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

_sessions_lock = threading.Lock()


def load_sessions():
    """Load sessions from disk."""
    if not os.path.isfile(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}


def save_sessions(sessions):
    """Save sessions to disk."""
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)


def get_session(thread_ts):
    """Get session info for a thread, or None."""
    with _sessions_lock:
        sessions = load_sessions()
        return sessions.get(thread_ts)


def set_session(thread_ts, session_id, project_dir):
    """Store a session mapping."""
    with _sessions_lock:
        sessions = load_sessions()
        sessions[thread_ts] = {
            "session_id": session_id,
            "project_dir": project_dir,
            "last_active": time.time(),
        }
        # Clean old entries
        cutoff = time.time() - SESSION_MAX_AGE
        sessions = {
            ts: info for ts, info in sessions.items()
            if info.get("last_active", 0) > cutoff
        }
        save_sessions(sessions)


def touch_session(thread_ts):
    """Update last_active timestamp for a session."""
    with _sessions_lock:
        sessions = load_sessions()
        if thread_ts in sessions:
            sessions[thread_ts]["last_active"] = time.time()
            save_sessions(sessions)


# ---------------------------------------------------------------------------
# Project directory resolution
# ---------------------------------------------------------------------------

def resolve_project_and_prompt(text):
    """
    Parse the message text to extract a project directory and prompt.

    Formats:
      /path/to/dir prompt here   → explicit path
      projectname: prompt here   → shorthand from PROJECT_* in .env
      just the prompt            → uses DEFAULT_PROJECT_DIR

    Returns (project_dir, prompt).
    """
    text = text.strip()

    # Check for explicit absolute path prefix
    match = re.match(r'^(/\S+)\s+(.+)', text, re.DOTALL)
    if match:
        path_candidate = match.group(1)
        if os.path.isdir(path_candidate):
            return path_candidate, match.group(2).strip()

    # Check for shorthand prefix (e.g., "calabrio: prompt" or "calabrio prompt")
    match = re.match(r'^(\w+):\s*(.+)', text, re.DOTALL)
    if match:
        shorthand = match.group(1).lower()
        if shorthand in PROJECT_MAP:
            return PROJECT_MAP[shorthand], match.group(2).strip()

    # Also match without colon (e.g., "calabrio list custom objects")
    match = re.match(r'^(\w+)\s+(.+)', text, re.DOTALL)
    if match:
        shorthand = match.group(1).lower()
        if shorthand in PROJECT_MAP:
            return PROJECT_MAP[shorthand], match.group(2).strip()

    # Default
    return DEFAULT_PROJECT_DIR, text


# ---------------------------------------------------------------------------
# Claude Code CLI execution
# ---------------------------------------------------------------------------

def run_claude(prompt, project_dir, session_id=None, thread_ts=None):
    """
    Run Claude Code CLI and return (response_text, new_session_id).

    Uses --output-format json to get structured output.
    Sets SLACK_THREAD_TS so the approval hook posts in the same thread.
    """
    # Wrap prompt with Slack formatting instruction
    slack_prompt = (
        f"{prompt}\n\n"
        "IMPORTANT: Your response will be displayed in Slack. Format accordingly:\n"
        "- Use bullet points (- or *) instead of markdown tables\n"
        "- Use *bold* for emphasis (Slack syntax)\n"
        "- Use `backticks` for code/API names\n"
        "- Do NOT use markdown tables (| col | col |) — Slack doesn't render them\n"
        "- Do NOT use ## headings — use *Bold Text* on its own line instead\n"
        "- Keep responses concise"
    )
    cmd = ["claude", "-p", slack_prompt, "--output-format", "json", "--dangerously-skip-permissions"]

    if session_id:
        cmd.extend(["--resume", session_id])

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Allow nested launch outside current session
    env["SLACK_THREAD_TS"] = thread_ts or ""

    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute max per invocation
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ":warning: Claude timed out after 10 minutes.", session_id
    except FileNotFoundError:
        return ":x: `claude` CLI not found. Is it installed and on PATH?", session_id
    except Exception as exc:
        return f":x: Error running Claude: {exc}", session_id

    # Parse JSON output
    stdout = result.stdout.strip()
    if not stdout:
        stderr = result.stderr.strip()
        if stderr:
            return f":warning: Claude returned no output.\n```\n{stderr[:2000]}\n```", session_id
        return ":warning: Claude returned no output.", session_id

    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        # Not JSON — return raw text (might be plain text mode)
        return stdout[:4000], session_id

    # Extract response text and session ID from JSON output
    new_session_id = output.get("session_id", session_id)
    response_text = output.get("result", "")

    if not response_text:
        # Try alternate field names
        response_text = output.get("content", output.get("text", ""))

    if not response_text:
        response_text = json.dumps(output, indent=2)[:4000]

    return response_text, new_session_id


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

# Track threads currently being processed to avoid duplicate handling
_active_threads = set()
_active_threads_lock = threading.Lock()


def handle_message(text, thread_ts, is_thread_reply, user_id):
    """Handle a single Slack message — run Claude and post the response."""
    # Prevent concurrent handling of the same thread
    with _active_threads_lock:
        if thread_ts in _active_threads:
            return
        _active_threads.add(thread_ts)

    try:
        _process_message(text, thread_ts, is_thread_reply)
    finally:
        with _active_threads_lock:
            _active_threads.discard(thread_ts)


def _process_message(text, thread_ts, is_thread_reply):
    """Process a message: resolve project, run Claude, post response."""
    # Post "thinking" indicator
    thinking_ts = post_message(":hourglass_flowing_sand: Claude is thinking...", thread_ts=thread_ts)

    if is_thread_reply:
        # Continue existing session
        session = get_session(thread_ts)
        if session:
            session_id = session["session_id"]
            project_dir = session["project_dir"]
            response, new_session_id = run_claude(text, project_dir, session_id=session_id, thread_ts=thread_ts)
            if new_session_id:
                set_session(thread_ts, new_session_id, project_dir)
            else:
                touch_session(thread_ts)
        else:
            # No session found for this thread — treat as new
            project_dir, prompt = resolve_project_and_prompt(text)
            response, new_session_id = run_claude(prompt, project_dir, thread_ts=thread_ts)
            if new_session_id:
                set_session(thread_ts, new_session_id, project_dir)
    else:
        # New conversation
        project_dir, prompt = resolve_project_and_prompt(text)
        response, new_session_id = run_claude(prompt, project_dir, thread_ts=thread_ts)
        if new_session_id:
            set_session(thread_ts, new_session_id, project_dir)

    # Delete the "thinking" message, then post the real response
    if thinking_ts:
        slack_post("chat.delete", {
            "channel": SLACK_CHANNEL_ID,
            "ts": thinking_ts,
        })

    # Post response as new message(s) in thread (4000 char Slack limit)
    remaining = response
    while remaining:
        chunk = remaining[:4000]
        remaining = remaining[4000:]
        post_message(chunk, thread_ts=thread_ts)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def _get_latest_channel_ts():
    """Get the timestamp of the most recent message in the channel.
    Uses Slack's timestamps instead of system time to avoid clock skew."""
    resp = slack_get("conversations.history", {
        "channel": SLACK_CHANNEL_ID,
        "limit": "1",
    })
    if resp.get("ok") and resp.get("messages"):
        return resp["messages"][0]["ts"]
    # Fallback: start from beginning — safe since seen_messages deduplicates
    return "0"


def main():
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        print("ERROR: Missing SLACK_BOT_TOKEN or SLACK_CHAT_CHANNEL_ID in .env", file=sys.stderr)
        sys.exit(1)

    bot_user_id = get_bot_user_id()
    if not bot_user_id:
        print("ERROR: Could not get bot user ID. Check SLACK_BOT_TOKEN.", file=sys.stderr)
        sys.exit(1)

    print(f"Claude Slack Daemon started")
    print(f"  Bot user ID: {bot_user_id}")
    print(f"  Channel: {SLACK_CHANNEL_ID}")
    print(f"  Default project: {DEFAULT_PROJECT_DIR}")
    print(f"  Project shortcuts: {PROJECT_MAP}")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    print(f"  Listening for messages...\n")

    # Use latest Slack message ts as baseline (avoids system clock skew)
    last_ts = _get_latest_channel_ts()
    print(f"  Baseline ts: {last_ts}")
    # Track seen messages by ts to avoid duplicates
    seen_messages = set()
    seen_thread_messages = set()

    while True:
        try:
            new_last_ts = _poll_cycle(bot_user_id, last_ts, seen_messages, seen_thread_messages)
            if new_last_ts:
                last_ts = new_last_ts
        except KeyboardInterrupt:
            print("\nDaemon stopped.")
            break
        except Exception as exc:
            print(f"Poll error: {exc}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)


def _poll_cycle(bot_user_id, last_ts, seen_messages, seen_thread_messages):
    """One polling cycle: check for new messages and thread replies.
    Returns the newest message ts seen, or None."""
    # Check for new top-level messages
    resp = slack_get("conversations.history", {
        "channel": SLACK_CHANNEL_ID,
        "oldest": last_ts,
        "limit": "10",
    })

    if not resp.get("ok"):
        return None

    messages = resp.get("messages", [])
    newest_ts = None

    for msg in messages:
        user = msg.get("user", "")
        text = msg.get("text", "").strip()
        ts = msg.get("ts", "")
        thread_ts = msg.get("thread_ts")

        # Track the newest ts we've seen
        if not newest_ts or ts > newest_ts:
            newest_ts = ts

        # Skip already-seen messages
        if ts in seen_messages:
            continue
        seen_messages.add(ts)

        # Skip bot's own messages
        if user == bot_user_id:
            continue
        # Skip messages with subtypes (joins, bot messages, etc.)
        if msg.get("subtype"):
            continue
        # Skip empty messages
        if not text:
            continue
        # Skip thread replies here — we handle them separately
        if thread_ts:
            continue
        # Strip bot @mentions from the text
        text = re.sub(r'<@[A-Z0-9]+>\s*', '', text).strip()
        if not text:
            continue

        print(f"New message from {user}: {text[:80]}...")

        # Start handling in a new thread
        t = threading.Thread(
            target=handle_message,
            args=(text, ts, False, user),
            daemon=True,
        )
        t.start()

    # Check for thread replies on active sessions
    with _sessions_lock:
        sessions = load_sessions()
        active_threads = list(sessions.keys())

    for thread_ts in active_threads:
        resp = slack_get("conversations.replies", {
            "channel": SLACK_CHANNEL_ID,
            "ts": thread_ts,
            "limit": "10",
            "oldest": "0",
        })

        if not resp.get("ok"):
            continue

        thread_messages = resp.get("messages", [])

        for msg in thread_messages[1:]:  # Skip the original message
            msg_ts = msg.get("ts", "")
            user = msg.get("user", "")
            text = msg.get("text", "").strip()

            # Skip already-seen messages
            if msg_ts in seen_thread_messages:
                continue

            # Skip bot's own messages
            if user == bot_user_id:
                seen_thread_messages.add(msg_ts)
                continue
            # Skip subtypes
            if msg.get("subtype"):
                seen_thread_messages.add(msg_ts)
                continue
            # Skip empty
            if not text:
                seen_thread_messages.add(msg_ts)
                continue

            # Skip messages that look like approval hook messages (from the approver)
            if ":lock: *Claude Code" in text:
                seen_thread_messages.add(msg_ts)
                continue

            # Strip bot @mentions
            text = re.sub(r'<@[A-Z0-9]+>\s*', '', text).strip()
            if not text:
                seen_thread_messages.add(msg_ts)
                continue

            seen_thread_messages.add(msg_ts)
            print(f"Thread reply from {user} in {thread_ts[:10]}: {text[:80]}...")

            t = threading.Thread(
                target=handle_message,
                args=(text, thread_ts, True, user),
                daemon=True,
            )
            t.start()

    return newest_ts


if __name__ == "__main__":
    main()

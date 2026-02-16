#!/usr/bin/env bash
# Launch the Claude Slack Chat Daemon in a tmux session.
#
# Usage:
#   ./start-daemon.sh          # start (or restart) the daemon
#   ./start-daemon.sh stop     # stop the daemon
#   ./start-daemon.sh status   # check if running
#   ./start-daemon.sh logs     # attach to see live output

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION_NAME="claude-daemon"
DAEMON_SCRIPT="$SCRIPT_DIR/claude-slack-daemon.py"

case "${1:-start}" in
    start)
        # Kill existing session if any
        tmux kill-session -t "$SESSION_NAME" 2>/dev/null
        echo "Starting Claude Slack Daemon..."
        tmux new-session -d -s "$SESSION_NAME" "python3 '$DAEMON_SCRIPT'"
        echo "Daemon running in tmux session '$SESSION_NAME'"
        echo "  View logs:  tmux attach -t $SESSION_NAME"
        echo "  Stop:       $0 stop"
        ;;
    stop)
        tmux kill-session -t "$SESSION_NAME" 2>/dev/null
        echo "Daemon stopped."
        ;;
    status)
        if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
            echo "Daemon is RUNNING (tmux session: $SESSION_NAME)"
        else
            echo "Daemon is NOT running."
        fi
        ;;
    logs)
        tmux attach -t "$SESSION_NAME"
        ;;
    *)
        echo "Usage: $0 {start|stop|status|logs}"
        exit 1
        ;;
esac

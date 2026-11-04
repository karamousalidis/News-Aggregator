#!/bin/bash
set -euo pipefail

LABEL="com.ai-news-digest"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$PROJECT_DIR/.venv/bin/python"
SCRIPT="$PROJECT_DIR/news_digest.py"
UID_NUMBER="$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$UID_NUMBER" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL."
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Python not found at $PYTHON"
  echo "Create the environment first: python3 -m venv .venv"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"
"$PYTHON" - "$PLIST" "$PROJECT_DIR" "$SCRIPT" "$LABEL" <<'PY'
import pathlib
import plistlib
import sys

plist, project, script, label = sys.argv[1:]
root = pathlib.Path(project)
settings = {
    "Label": label,
    "ProgramArguments": [str(root / ".venv" / "bin" / "python"), script],
    "WorkingDirectory": project,
    "StartCalendarInterval": {"Hour": 8, "Minute": 15},
    "StandardOutPath": str(root / "logs" / "digest.log"),
    "StandardErrorPath": str(root / "logs" / "digest-error.log"),
}
with open(plist, "wb") as output:
    plistlib.dump(settings, output)
PY

launchctl bootout "gui/$UID_NUMBER" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUMBER" "$PLIST"
echo "Scheduled $LABEL daily at 08:15 local time."

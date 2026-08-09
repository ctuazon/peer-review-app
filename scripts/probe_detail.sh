#!/bin/bash
CLAUDE=$(find "$HOME/.vscode-server/extensions" -path '*/native-binary/claude' -type f 2>/dev/null | sort | tail -n 1)
APP='/mnt/c/Users/Czyrus.Tuazon/Desktop/Peer Review App'
printf '%s\n' 'Use extended thinking. Briefly reason about 2+2, then answer with the number only.' > /tmp/t.txt
timeout 90 "$CLAUDE" -p "$(cat /tmp/t.txt)" \
  --output-format stream-json --verbose --include-partial-messages --effort high \
  2>/dev/null | python3 "$APP/scripts/parse_stream_detail.py"

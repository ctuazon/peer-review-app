#!/bin/bash
set -e
CLAUDE=$(find "$HOME/.vscode-server/extensions" -path '*/native-binary/claude' -type f 2>/dev/null | sort | tail -n 1)
APP='/mnt/c/Users/Czyrus.Tuazon/Desktop/Peer Review App'
printf '%s\n' 'Think step by step about whether 17 is prime, then answer yes or no.' > /tmp/peer-review-think-test.txt
timeout 120 "$CLAUDE" -p "$(cat /tmp/peer-review-think-test.txt)" \
  --output-format stream-json --verbose --include-partial-messages --effort high \
  2>/tmp/peer-review-think-err.txt \
  | python3 "$APP/scripts/parse_stream.py"
echo '==== ERR ===='
head -n 10 /tmp/peer-review-think-err.txt || true

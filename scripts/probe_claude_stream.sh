#!/bin/bash
CLAUDE=$(find "$HOME/.vscode-server/extensions" -path '*/native-binary/claude' -type f 2>/dev/null | sort | tail -n 1)
"$CLAUDE" -h 2>&1 | grep -Ei 'output-format|verbose|partial|stream|thinking' || true
echo '==== SAMPLE STREAM ===='
# Tiny non-review prompt to inspect event shapes (may take a bit / use network)
printf '%s\n' 'Reply with exactly: hi' > /tmp/peer-review-stream-test.txt
timeout 90 "$CLAUDE" -p "$(cat /tmp/peer-review-stream-test.txt)" --output-format stream-json --verbose 2>/tmp/peer-review-stream-err.txt | head -n 40
echo '==== STDERR ===='
head -n 20 /tmp/peer-review-stream-err.txt

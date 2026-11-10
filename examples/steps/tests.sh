#!/bin/sh
# Run the test command. Exit non-zero, with the tail of its output on stderr, when it fails.
cmd="$(jq -r '.test_command')"
log="$(sh -c "$cmd" 2>&1)" && rc=0 || rc=$?
if [ "$rc" -ne 0 ]; then printf '%s\n' "$log" | tail -20 >&2; exit "$rc"; fi
jq -nc --arg files "$(git status --porcelain | wc -l | tr -d ' ')" '{passed: true, changed_files: ($files | tonumber)}'

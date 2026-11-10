#!/bin/sh
# A rejected change ends the run as failed, so "completed" always means approved.
d="$(jq -r '.approve.decision // empty')"
[ "$d" = approve ] && echo '{"approved":true}' || { echo "the change was not approved (decision: ${d:-none})" >&2; exit 1; }

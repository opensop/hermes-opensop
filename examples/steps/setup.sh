#!/bin/sh
# Record the starting commit. The change step must start from a clean tree.
set -e
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git checkout: $PWD" >&2; exit 1; }
[ -z "$(git status --porcelain -- . ':!.opensop')" ] || { echo "working tree is not clean; commit or stash first" >&2; exit 1; }
printf '{"base":"%s"}\n' "$(git rev-parse HEAD)"

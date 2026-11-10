#!/bin/sh
# Record the starting commit. The change step must start from a clean tree.
set -e
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git checkout: $PWD. Ask the user which project to use; don't create a repository." >&2; exit 1; }
[ -z "$(git status --porcelain -- . ':!.opensop')" ] || { echo "the working tree has uncommitted changes. Ask the user to commit or stash them; don't do it for them." >&2; exit 1; }
printf '{"base":"%s"}\n' "$(git rev-parse HEAD)"

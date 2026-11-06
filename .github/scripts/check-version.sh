#!/bin/bash
# Checks that a release tag matches the version numbers in the repo, and prints the version.
#   .github/scripts/check-version.sh v0.2.0        -> prints 0.2.0, or fails naming each file that differs
# A pre-release tag (v0.2.0-rc1) is checked against its base version (0.2.0).
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.."

tag="${1:?usage: check-version.sh <tag, e.g. v0.2.0>}"
if [[ ! $tag =~ ^v([0-9]+\.[0-9]+\.[0-9]+)(-[0-9A-Za-z.]+)?$ ]]; then
  echo "::error::Tag '$tag' isn't a version tag like v0.2.0 or v0.2.0-rc1." >&2
  exit 1
fi
want="${BASH_REMATCH[1]}"

# file|what it holds|the version found there
app="$(sed -nE 's/^[[:space:]]*readonly property string version: "([^"]*)".*/\1/p' AppState.qml | head -n1)"
mcp="$(sed -nE 's/^version = "([^"]*)".*/\1/p' mcp/pyproject.toml | head -n1)"

bad=0
for entry in "AppState.qml|the version shown in Settings › About|$app" "mcp/pyproject.toml|the MCP server's version|$mcp"; do
  IFS='|' read -r file what have <<<"$entry"
  if [[ $have != "$want" ]]; then
    echo "::error file=$file::$file has ${have:-no version} ($what), but the tag $tag needs $want. Bump it, commit, and tag that commit." >&2
    bad=1
  fi
done
((bad == 0)) || exit 1
echo "$want"

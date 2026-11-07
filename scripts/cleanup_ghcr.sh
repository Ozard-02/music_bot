#!/usr/bin/env bash
# Delete old versions of the GHCR container package, keeping the newest N.
#
# Usage:
#   GITHUB_TOKEN=ghp_xxx ./scripts/cleanup_ghcr.sh
#   GITHUB_TOKEN=ghp_xxx KEEP=5 ./scripts/cleanup_ghcr.sh
#
# The token needs the classic `read:packages` and `delete:packages` scopes.
# Owner must match the token owner (the package lives under /user).
set -euo pipefail

OWNER="${OWNER:-Ozard-02}"
PACKAGE="${PACKAGE:-music_loop}"
KEEP="${KEEP:-10}"
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN to a PAT with read:packages + delete:packages}"

API="https://api.github.com"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

page=1
while :; do
  body="$(curl -fsSL \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "$API/user/packages/container/$PACKAGE/versions?per_page=100&page=$page")"
  count="$(printf '%s' "$body" | jq 'length')"
  [ "$count" -eq 0 ] && break
  printf '%s' "$body" | jq -r \
    '.[] | [.id, .created_at, ((.metadata.container.tags // []) | join(","))] | @tsv' \
    >> "$tmp"
  [ "$count" -lt 100 ] && break
  page=$((page + 1))
done

total="$(wc -l < "$tmp" | tr -d ' ')"
echo "Found $total versions; keeping the newest $KEEP."

# Newest first, skip the first KEEP, delete the rest.
tail -n "+$((KEEP + 1))" < <(sort -t$'\t' -k2,2r "$tmp") |
  while IFS=$'\t' read -r id created tags; do
    echo "Deleting $id ($created) tags=[$tags]"
    curl -fsSL -X DELETE \
      -H "Authorization: Bearer $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      "$API/user/packages/container/$PACKAGE/versions/$id" >/dev/null
  done

echo "Done."

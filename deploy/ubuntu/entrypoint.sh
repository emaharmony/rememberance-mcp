#!/bin/sh
set -eu

source_token=/run/secrets/recall-api-token
target_token=/var/lib/recall/secrets/api-token
if [ -f "$source_token" ]; then
  install -d -m 700 "$(dirname "$target_token")"
  install -m 600 "$source_token" "$target_token"
fi

exec recall-service "$@"

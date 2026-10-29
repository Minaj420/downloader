#!/usr/bin/env bash
set -euo pipefail
exec python3 -I "$(dirname "$0")/claude-agent.py" "$@"

#!/usr/bin/env bash
# Compatibility entrypoint: read-only Opus review, never implementation.
set -euo pipefail
exec python3 -I "$(dirname "$0")/claude-agent.py" review "$@"

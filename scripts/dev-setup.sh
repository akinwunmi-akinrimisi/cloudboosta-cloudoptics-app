#!/usr/bin/env bash
# One-time setup for Codespaces / dev containers: tools and dependencies.
set -euo pipefail
pip install --quiet uv
corepack enable
uv sync --all-packages
pnpm install

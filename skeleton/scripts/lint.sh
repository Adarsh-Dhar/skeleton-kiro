#!/bin/sh
# Lint + format check for the whole workspace (same rules as CI).
set -e
cd "$(dirname "$0")/.."
ruff check .
ruff format --check .

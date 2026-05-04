#!/usr/bin/env bash
# Quick-start script for Freyja development.
# Usage: ./launch.sh
#
# Prerequisites:
#   1. Node.js ≥ 18 and npm
#   2. Python ≥ 3.11 and uv (https://docs.astral.sh/uv/)
#   3. Rust toolchain (for the native extension — only needed once)
#   4. A .env file with at least ANTHROPIC_API_KEY set
#
# First run:
#   cp .env.example .env   # fill in your API key
#   uv sync                # install Python deps
#   npm install             # install JS deps
#   cd native/freyja_native && maturin develop --release && cd ../..
#   ./launch.sh

set -euo pipefail
cd "$(dirname "$0")"

# Ensure .env exists
if [ ! -f .env ]; then
    echo "error: no .env file found. Copy .env.example to .env and add your API keys."
    exit 1
fi

# Install deps if needed
if [ ! -d node_modules ]; then
    echo "Installing JS dependencies..."
    npm install
fi

if [ ! -d .venv ]; then
    echo "Setting up Python venv..."
    uv sync
fi

# Start the Electron app in dev mode
echo "Starting Freyja..."
npm run dev

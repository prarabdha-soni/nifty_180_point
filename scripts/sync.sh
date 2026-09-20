#!/bin/bash
# Pull the latest code and bar cache from GitHub. The cloud fetch job owns
# data/raw/angel/; local test runs may rewrite the current (non-final) block,
# and those local copies are disposable -- discard them before pulling.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git checkout -- data/raw/angel 2>/dev/null || true
git pull --rebase origin main

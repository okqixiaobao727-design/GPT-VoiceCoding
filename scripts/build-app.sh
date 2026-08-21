#!/usr/bin/env bash
#
# One command, from a clean checkout, to a signed GPT-VoiceCoding.app.
#
#   scripts/build-app.sh                  # the shipping bundle
#   scripts/build-app.sh --debug          # a debug build of the shell
#   scripts/build-app.sh --without-engine # the shell alone, for the developer loop
#   scripts/build-app.sh lock             # regenerate the dependency lock
#
# There is deliberately nothing else here. The pipeline is `app_bundle/`, in
# Python, under the project's own test suite — because the decisions it makes
# (which Mach-O files exist, and in what order they are signed) are the ones
# `codesign --verify` cannot check for us, and a decision in bash is a decision
# no test can hold still.
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec python3 -m app_bundle "$@"

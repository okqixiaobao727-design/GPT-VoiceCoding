#!/usr/bin/env bash
#
# Assemble a DEVELOPMENT app bundle for local proof, and nothing more.
#
# This is a dev artifact — including its `--engine` flag. It exists because the
# shell cannot be shown to work outside an .app at all (`MenuBarExtra`,
# `LSUIElement`, `SMAppService.mainApp` and the microphone grant all need a real
# bundle), and because bundle containment is the load-bearing claim of ADR 0005:
# a shell that only ever ran the developer fallback would have demonstrated the
# optional path and skipped the constitutive one.
#
# #12 (the app bundle and signing pipeline) owns distribution and MAY REPLACE
# THIS SCRIPT WHOLESALE. Do not treat it as a base to extend. `--engine` COPIES
# an interpreter tree you already have: it downloads nothing, vendors nothing,
# signs nothing beyond the one ad-hoc step below, and decides no binary name.
# The real pipeline — python-build-standalone, the inside-out enumerate-and-sign
# over ~82 Mach-O files, entitlements on the bundled interpreter — is #12's, and
# none of it is here.
#
# Usage:
#   shell/scripts/dev-app.sh [debug|release] [--engine <interpreter-root>]
#
# <interpreter-root> is a directory containing `bin/python3` that can import
# gpt_voicecoding — a virtual environment works. It is copied to
# Contents/Resources/engine/, which is where the shell looks first.
#
set -euo pipefail

configuration="debug"
engine_root=""
while [ $# -gt 0 ]; do
    case "$1" in
        debug|release) configuration="$1"; shift ;;
        --engine) engine_root="${2:?--engine needs an interpreter root}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

swift build --package-path "$here" -c "$configuration" --product GPTVoiceCodingShell

binary="$(swift build --package-path "$here" -c "$configuration" --show-bin-path)/GPTVoiceCodingShell"
app="$here/.build/GPT-VoiceCoding.app"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$binary" "$app/Contents/MacOS/GPTVoiceCodingShell"
cp "$here/Resources/Info.plist" "$app/Contents/Info.plist"

if [ -n "$engine_root" ]; then
    if [ ! -x "$engine_root/bin/python3" ]; then
        echo "no bin/python3 under $engine_root" >&2
        exit 2
    fi
    # Dereferenced, because a virtual environment's bin/python3 is usually a
    # symlink out of the bundle — and a link pointing outside is not containment.
    cp -RL "$engine_root/" "$app/Contents/Resources/engine/"
fi

# Ad-hoc, because v0 ships no Developer ID and is not notarized — a charter
# decision, with the known cost that the signature changes per build and macOS
# may ask for the microphone again after one.
codesign --force --sign - --timestamp=none "$app"
codesign --verify --deep --strict "$app"

cat <<EOF

Built $app

  open "$app"

EOF

if [ -n "$engine_root" ]; then
    cat <<EOF
An interpreter is bundled at Contents/Resources/engine/bin/python3, so the shell
takes the bundled path — the one ADR 0005 is about. To see the child under the
shell rather than under launchd:

  pgrep -P \$(pgrep -f MacOS/GPTVoiceCodingShell | head -1)

And, as root, the attribution the TCC probe checked:

  sudo launchctl procinfo \$(pgrep -f gpt_voicecoding.engine | head -1) | grep -i responsible

EOF
else
    cat <<EOF
No engine is bundled, so this build takes the developer path:
GPTVOICECODING_ENGINE_PYTHON, or the first python3 on PATH, running
\`-m gpt_voicecoding.engine --config <the engine's config.toml>\`. Headless mode
is a stated feature, not a stopgap — the engine runs standalone either way.

Pass --engine <interpreter-root> to exercise the bundled path instead.

EOF
fi

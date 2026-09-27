#!/usr/bin/env bash
# Build ScreenAgent and wrap it in build/ScreenAgent.app so macOS treats it as a real app
# (own mic permission prompt, Dock icon). No Xcode needed: SwiftPM + codesign only.
#   CONFIG=debug scripts/bundle.sh   # debug build (default: release)
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${CONFIG:-release}"
APP="build/ScreenAgent.app"

swift build -c "$CONFIG" --product ScreenAgent
BIN_DIR="$(swift build -c "$CONFIG" --show-bin-path)"

# Assemble and sign outside the repo: when it lives in an iCloud-synced folder (e.g.
# ~/Desktop), Finder metadata gets attached to new files asynchronously and codesign
# intermittently refuses with "resource fork, Finder information ... not allowed".
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
STAGED_APP="$STAGE/ScreenAgent.app"
mkdir -p "$STAGED_APP/Contents/MacOS" "$STAGED_APP/Contents/Resources"
cp "$BIN_DIR/ScreenAgent" "$STAGED_APP/Contents/MacOS/ScreenAgent"
cp Resources/Info.plist "$STAGED_APP/Contents/Info.plist"
plutil -lint "$STAGED_APP/Contents/Info.plist" >/dev/null
xattr -cr "$STAGED_APP"

# Ad-hoc signature: enough for TCC to attribute the mic prompt to this app. The signature
# changes on every rebuild, so macOS may ask for mic access again after rebuilding.
codesign --force --sign - --timestamp=none "$STAGED_APP"
codesign --verify --strict "$STAGED_APP"

rm -rf "$APP"
mkdir -p build
ditto "$STAGED_APP" "$APP"
codesign --verify "$APP"

echo "Built $APP ($CONFIG)"

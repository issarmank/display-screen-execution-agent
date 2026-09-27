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

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN_DIR/ScreenAgent" "$APP/Contents/MacOS/ScreenAgent"
cp Resources/Info.plist "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

# Ad-hoc signature: enough for TCC to attribute the mic prompt to this app. The signature
# changes on every rebuild, so macOS may ask for mic access again after rebuilding.
codesign --force --sign - --timestamp=none "$APP"
codesign --verify --strict "$APP"

echo "Built $APP ($CONFIG)"

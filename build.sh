#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p runtime/module-cache runtime/MiniToo.app/Contents/MacOS
swiftc -module-cache-path "$PWD/runtime/module-cache" transport/divoom-send.swift -o runtime/MiniToo.app/Contents/MacOS/divoom-send
cat > runtime/MiniToo.app/Contents/Info.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>divoom-send</string>
<key>CFBundleIdentifier</key><string>local.codex.minitoo</string>
<key>CFBundleName</key><string>Codex MiniToo</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
<key>NSBluetoothAlwaysUsageDescription</key><string>Display Codex status on your Divoom MiniToo.</string>
<key>NSBluetoothPeripheralUsageDescription</key><string>Display Codex status on your Divoom MiniToo.</string>
</dict></plist>
PLIST
codesign --force --sign - runtime/MiniToo.app
swiftc -module-cache-path "$PWD/runtime/module-cache" transport/render-status.swift -o runtime/render-status
runtime/render-status "$PWD/runtime/screens"

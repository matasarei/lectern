#!/usr/bin/env bash
# Build Lectern's menu-bar app for macOS: compile tray/*.swift with swiftc (the Command Line
# Tools are enough; there's no Xcode project), have it draw its own icon, write its
# Info.plist, sign it ad hoc.
#
#   build_app.sh <bundle> <install folder> <version>
#   build_app.sh ~/Applications/Lectern.app ~/.lectern 0.1.0
#
# Whatever is at <bundle> is replaced, so it must be a path ending in .app (install.sh builds
# into Lectern.app.new and moves it in). The install folder is written into the app, which
# starts <install folder>/bin/lectern-serve. LECTERN_SWIFTC names another swiftc.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
die() { echo "build_app.sh: $*" >&2; exit 1; }

[ $# -eq 3 ] || die "usage: build_app.sh <bundle> <install folder> <version>"
bundle="$1" dir="$2" version="$3"

case "$bundle" in
    *.app | *.app.new) ;;
    *) die "the bundle must be a path ending in .app (it's replaced): $bundle" ;;
esac
# Both go into Info.plist through sed: plain paths and versions only.
case "$dir" in
    /*) ;;
    *) die "the install folder must be an absolute path: $dir" ;;
esac
case "$dir" in
    *[!A-Za-z0-9._/+@-]*) die "the install folder can't hold spaces or special characters: \"$dir\"" ;;
esac
case "$version" in
    *[!A-Za-z0-9._+-]* | "") die "not a version: \"$version\"" ;;
esac

swiftc="${LECTERN_SWIFTC:-swiftc}"
# On a Mac without the tools /usr/bin/swiftc is a stub that only asks to install them.
"$swiftc" --version >/dev/null 2>&1 || die "building the app needs the Command Line Tools \
(swiftc): xcode-select --install"

rm -rf "$bundle"
mkdir -p "$bundle/Contents/MacOS"
"$swiftc" -O -swift-version 5 -parse-as-library -target arm64-apple-macos13.0 \
    -o "$bundle/Contents/MacOS/Lectern" "$here/tray/Lectern.swift" "$here/tray/Icon.swift" \
    || { rm -rf "$bundle"; die "compiling the app failed (see above)."; }
# The icon is drawn by the app itself (tray/Icon.swift), here, with this Mac's own fonts: no
# artwork is kept in the repository. Without it the app works and shows the system's icon.
iconset="$(mktemp -d)/Lectern.iconset"
mkdir -p "$bundle/Contents/Resources"
if ! { "$bundle/Contents/MacOS/Lectern" --draw-icon "$iconset" \
       && iconutil -c icns -o "$bundle/Contents/Resources/Lectern.icns" "$iconset"; } 2>/dev/null; then
    echo "build_app.sh: couldn't draw the app's icon; it will show the system's." >&2
    rm -rf "$bundle/Contents/Resources"
fi
rm -rf "$(dirname "$iconset")"
sed -e "s|@VERSION@|$version|g" -e "s|@LECTERN_DIR@|$dir|g" "$here/tray/Info.plist" \
    > "$bundle/Contents/Info.plist"
# Ad hoc: no developer identity. Apple Silicon runs nothing unsigned, and a bundle whose
# Info.plist changed after its binary was signed doesn't verify.
codesign --force --sign - "$bundle" 2>/dev/null \
    || { rm -rf "$bundle"; die "signing the app failed (codesign --force --sign - $bundle)."; }

#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SOURCE="$SCRIPT_DIR/trcc_usb_power.c"
OUTPUT="$SCRIPT_DIR/trcc-usb-power"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "trcc-usb-power is macOS-only" >&2
    exit 1
fi

xcrun clang -std=c11 -Wall -Wextra -Werror \
    -framework IOKit -framework CoreFoundation \
    "$SOURCE" -o "$OUTPUT"
codesign -s - "$OUTPUT"
"$OUTPUT" status

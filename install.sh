#!/usr/bin/env bash
# Installs or updates pluk_notify in a Hermes installation. Run it ON THE SERVER, as the user Hermes runs as,
# from the folder this file is in:
#
#     bash install.sh --dry-run     # shows what it would do, changes nothing
#     bash install.sh               # does it
#
# What it does: copies the plugin to <hermes>/plugins/pluk_notify, links it into every profile's plugins/ folder
# (Hermes loads a profile's plugins from the profile's own folder only), creates <hermes>/pluk-notify/ (mode 700)
# for the two Firebase files, and turns the plugin on in the main config and in each profile.
# What it never does: restart a service, open a port, touch a firewall or a reverse proxy, or read a secret.
set -euo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${HERMES_HOME:-$HOME/.hermes}"
NAME="pluk_notify"

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "  would run: $*"; else "$@"; fi; }

[ -f "$HERE/plugin.yaml" ] || { say "Run this from the plugin's own folder (plugin.yaml is not here)."; exit 1; }
[ -d "$ROOT" ] || { say "No Hermes home at $ROOT. Set HERMES_HOME and run again."; exit 1; }
command -v hermes >/dev/null 2>&1 || { say "The 'hermes' command is not on the PATH of this user."; exit 1; }

VERSION="$(sed -n 's/^version:[[:space:]]*//p' "$HERE/plugin.yaml" | tr -d "\"'")"
say "pluk_notify $VERSION -> $ROOT"
[ "$DRY" = 1 ] && say "(dry run: nothing is changed)"

# 1. The one copy of the code.
TARGET="$ROOT/plugins/$NAME"
if [ "$HERE" != "$TARGET" ]; then
  say "1. Copy the plugin to $TARGET"
  run mkdir -p "$ROOT/plugins"
  if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    run rm -rf "$TARGET.previous"
    run mv "$TARGET" "$TARGET.previous"
  fi
  run cp -R "$HERE" "$TARGET"
  run rm -rf "$TARGET/__pycache__" "$TARGET/dashboard/__pycache__"
  [ -e "$TARGET.previous" ] && run rm -rf "$TARGET.previous"
else
  say "1. The plugin is already in place at $TARGET"
fi

# 2. Every profile runs that same copy.
say "2. Link it into each profile"
PROFILES=()
if [ -d "$ROOT/profiles" ]; then
  for dir in "$ROOT"/profiles/*/; do
    [ -f "$dir/config.yaml" ] || continue
    profile="$(basename "$dir")"
    PROFILES+=("$profile")
    link="$dir/plugins/$NAME"
    run mkdir -p "$dir/plugins"
    if [ -L "$link" ]; then
      run ln -sfn "$TARGET" "$link"
    elif [ -e "$link" ]; then
      say "   $profile has its own copy: kept aside as plugins-backup/$NAME"
      run mkdir -p "$dir/plugins-backup"
      run rm -rf "$dir/plugins-backup/$NAME"
      run mv "$link" "$dir/plugins-backup/$NAME"
      run ln -s "$TARGET" "$link"
    else
      run ln -s "$TARGET" "$link"
    fi
    say "   $profile"
  done
fi
[ "${#PROFILES[@]}" = 0 ] && say "   (no profile found: only the main Hermes)"

# 3. Where the two Firebase files go.
DATA="$ROOT/pluk-notify"
say "3. Folder for the Firebase files: $DATA"
run mkdir -p "$DATA"
run chmod 700 "$DATA"
for file in service-account.json google-services.json; do
  if [ -f "$DATA/$file" ]; then run chmod 600 "$DATA/$file"; say "   $file: present"; else say "   $file: MISSING (put it here)"; fi
done

# 4. Turn it on. Hermes may ask to confirm: answer yes.
say "4. Enable the plugin"
run hermes plugins enable "$NAME"
for profile in "${PROFILES[@]:-}"; do
  [ -n "$profile" ] && run hermes -p "$profile" plugins enable "$NAME"
done

say ""
say "Done. Nothing was restarted. The plugin loads when each service next starts:"
say "  - the Dashboard service (it serves the app's routes)"
say "  - each gateway service (it sends the notifications of its own agent)"
say "Restart them one at a time, each when its agent is not in the middle of a task, then check with:"
say "  hermes pluk-notify status"

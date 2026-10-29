#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  Re-deploy ONLY the themed configs from ./configs into ~/.config.
#
#  No packages, no repos, no login-manager changes, no sudo.  Use this after
#  editing anything under ./configs, or to pick up fixes without sitting
#  through the whole installer again.
#
#  Your current ~/.config copies are saved first, under
#  ~/.local/share/de-testlab/config-before-fix-<timestamp>/
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HOME/.local/share/de-testlab"
WALLPAPER="$HOME/.local/share/wallpapers/mocha-aurora.png"
DIRS="hypr sway awesome waybar wofi rofi kitty mako picom"

c_b=$'\e[1m'; c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_d=$'\e[2m'; c_0=$'\e[0m'
say()  { printf '\n%s▶ %s%s\n' "$c_b$c_g" "$*" "$c_0"; }
warn() { printf '%s! %s%s\n' "$c_y" "$*" "$c_0"; }
err()  { printf '%s✘ %s%s\n' "$c_r" "$*" "$c_0"; }
note() { printf '%s  %s%s\n' "$c_d" "$*" "$c_0"; }

[ -d "$SRC/configs" ] || { err "no configs/ next to this script ($SRC)"; exit 1; }

BACKUP="$STATE/config-before-fix-$(date +%Y%m%d-%H%M%S)"
say "Backing up what you have now"
mkdir -p "$BACKUP"
for d in $DIRS swaylock; do
  [ -e "$HOME/.config/$d" ] && cp -a "$HOME/.config/$d" "$BACKUP/$d"
done
note "$BACKUP"

say "Deploying configs"
mkdir -p "$HOME/.local/share/wallpapers" "$HOME/.local/bin"
[ -f "$SRC/configs/wallpapers/mocha-aurora.png" ] \
  && install -m644 "$SRC/configs/wallpapers/mocha-aurora.png" "$WALLPAPER"

KBLAYOUT=$(localectl status 2>/dev/null | awk '/X11 Layout/{print $3}'); KBLAYOUT=${KBLAYOUT:-us}
KBMODEL=$(localectl  status 2>/dev/null | awk '/X11 Model/{print $3}');  KBMODEL=${KBMODEL:-pc105}
note "keyboard: layout=$KBLAYOUT model=$KBMODEL"

for d in $DIRS; do
  [ -d "$SRC/configs/$d" ] || continue
  rm -rf "$HOME/.config/$d"
  mkdir -p "$HOME/.config/$d"
  cp -a "$SRC/configs/$d/." "$HOME/.config/$d/"
  find "$HOME/.config/$d" -type f -exec sed -i \
    -e "s|@WALLPAPER@|$WALLPAPER|g" \
    -e "s|@KBLAYOUT@|$KBLAYOUT|g" \
    -e "s|@KBMODEL@|$KBMODEL|g" {} +
  note "~/.config/$d"
done

mkdir -p "$HOME/.config/swaylock"
sed "s|@WALLPAPER@|$WALLPAPER|g" "$SRC/configs/sway/swaylock.conf" > "$HOME/.config/swaylock/config"
for f in "$SRC"/configs/bin/*; do
  [ -f "$f" ] && install -m755 "$f" "$HOME/.local/bin/$(basename "$f")"
done

say "Checking them"
if command -v awesome >/dev/null; then
  if awesome -k -c "$HOME/.config/awesome/rc.lua" >/dev/null 2>"$STATE/awesome-check.log"; then
    printf '%s✔ awesome rc.lua parses%s\n' "$c_g" "$c_0"
  else
    err "awesome rc.lua: $(tail -3 "$STATE/awesome-check.log")"
  fi
fi
if command -v sway >/dev/null; then
  SWAYOUT=$(sway --validate -c "$HOME/.config/sway/config" 2>&1)
  if printf '%s' "$SWAYOUT" | grep -qiE '^(Error|.*Error on line)'; then
    err "sway config errors:"; printf '%s\n' "$SWAYOUT" | grep -iE 'error' | head -5
  else
    printf '%s✔ sway config OK%s\n' "$c_g" "$c_0"
    printf '%s' "$SWAYOUT" | grep -i 'warning' | head -5
  fi
fi
python3 -c "import json,sys,re
for f in ['$HOME/.config/waybar/config.jsonc','$HOME/.config/waybar/config-sway.jsonc']:
    s=re.sub(r'(?m)^\s*//.*$','',open(f).read()); json.loads(s)
print('✔ waybar configs are valid JSON')" 2>/dev/null || warn "check waybar json"

if command -v hyprctl >/dev/null && [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
  say "Reloading Hyprland"
  hyprctl reload >/dev/null 2>&1
  ERRS=$(hyprctl configerrors 2>/dev/null)
  if [ -z "$ERRS" ] || printf '%s' "$ERRS" | grep -qi 'no errors'; then
    printf '%s✔ hyprland config has no errors%s\n' "$c_g" "$c_0"
  else
    warn "hyprland still reports:"; printf '%s\n' "$ERRS" | head -20
  fi
fi

say "Done"
note "Hyprland: Super+Shift+C reloads   SwayFX: Super+Shift+C reloads"
note "AwesomeWM: Super+Ctrl+R restarts (or log out and back in)"
note "Your previous configs: $BACKUP"

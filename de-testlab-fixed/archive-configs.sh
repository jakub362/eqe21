#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  Archive the de-testlab configs and get out of your way.
#
#  Packs every config this lab deployed into a dated tarball in your home
#  directory, removes them from ~/.config so you start from a clean slate,
#  and restores whatever you had there before the lab first ran.
#
#  It does NOT touch packages, repos or the login manager.  Every desktop
#  stays installed and selectable at login — you just get to configure them
#  from scratch, or drop someone else's dotfiles in.
#
#  Two things are deliberately left in place, because they are fixes rather
#  than theming.  Delete them yourself if you disagree:
#      ~/.config/autostart/de-testlab-keyring.desktop      your secret store
#      ~/.config/autostart/*xwaylandvideobridge*.desktop   the ghost window
#
#  Usage:  ./archive-configs.sh             asks once before doing anything
#          ./archive-configs.sh -y          no questions
#          ./archive-configs.sh --dry-run   print the plan, change nothing
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

STATE="$HOME/.local/share/de-testlab"
STAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE="$HOME/de-testlab-configs-$STAMP.tar.gz"
DIRS="hypr sway awesome waybar wofi rofi kitty mako picom swaylock"
BINS="de-files de-powermenu de-screenshot"
WALL=".local/share/wallpapers/mocha-aurora.png"

ASSUME_YES=0; DRY=0
for a in "$@"; do
  case "$a" in
    -y|--yes)     ASSUME_YES=1 ;;
    -n|--dry-run) DRY=1 ;;
    -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 0 ;;
    *) printf 'unknown option: %s  (try --help)\n' "$a"; exit 2 ;;
  esac
done

c_b=$'\e[1m'; c_g=$'\e[32m'; c_y=$'\e[33m'; c_d=$'\e[2m'; c_0=$'\e[0m'
say()  { printf '\n%s▶ %s%s\n' "$c_b$c_g" "$*" "$c_0"; }
warn() { printf '%s! %s%s\n' "$c_y" "$*" "$c_0"; }
note() { printf '%s  %s%s\n' "$c_d" "$*" "$c_0"; }
run()  { if [ "$DRY" = 1 ]; then printf '%s[dry-run]%s %s\n' "$c_d" "$c_0" "$*"; else "$@"; fi; }

# ── what is actually there ────────────────────────────────────────────────
# once we have restored someone's own config back into place, a later run
# must not treat it as ours and delete it again
RESTORED=""
[ -f "$STATE/config-backup/.restored" ] && \
  RESTORED=$(tr '\n' ' ' < "$STATE/config-backup/existed.txt" 2>/dev/null)
mine() { case " $RESTORED " in *" $1 "*) return 1 ;; *) return 0 ;; esac; }

REL=()
for d in $DIRS; do
  mine "$d" || { note "leaving ~/.config/$d alone — that one is yours, restored earlier"; continue; }
  [ -e "$HOME/.config/$d" ] && REL+=(".config/$d")
done
for b in $BINS; do [ -e "$HOME/.local/bin/$b" ] && REL+=(".local/bin/$b"); done
[ -e "$HOME/$WALL" ] && REL+=("$WALL")

if [ "${#REL[@]}" -eq 0 ]; then
  say "Nothing of this lab's config is in place — nothing to archive"
  exit 0
fi

say "Plan"
printf '  %sarchive to%s  %s\n' "$c_b" "$c_0" "$ARCHIVE"
printf '  %sthen remove%s\n' "$c_b" "$c_0"
for r in "${REL[@]}"; do printf '    ~/%s\n' "$r"; done
if [ -s "$STATE/config-backup/existed.txt" ]; then
  printf '  %sthen restore what you had before%s  %s\n' "$c_b" "$c_0" \
    "$(tr '\n' ' ' < "$STATE/config-backup/existed.txt")"
fi
printf '  %sleft alone%s  packages, repos, login manager, keyring autostart\n' "$c_b" "$c_0"
echo

if [ "$ASSUME_YES" != 1 ] && [ "$DRY" != 1 ]; then
  read -r -p "Type ${c_b}yes${c_0} to go ahead: " ans
  [ "$ans" = "yes" ] || { echo "aborted, nothing changed"; exit 0; }
fi

# ── 1. archive ────────────────────────────────────────────────────────────
say "Archiving"
if [ "$DRY" = 1 ]; then
  note "[dry-run] tar -czf $ARCHIVE -C $HOME ${REL[*]}"
else
  if tar -czf "$ARCHIVE" -C "$HOME" "${REL[@]}" 2>/dev/null; then
    note "$ARCHIVE  ($(du -h "$ARCHIVE" | cut -f1))"
  else
    warn "archiving failed — stopping here so nothing is lost"; exit 1
  fi
fi

# ── 2. remove ─────────────────────────────────────────────────────────────
say "Removing them from your home directory"
for r in "${REL[@]}"; do run rm -rf "$HOME/$r"; note "removed ~/$r"; done

# ── 3. put back whatever was there before the lab ran ─────────────────────
if [ -s "$STATE/config-backup/existed.txt" ]; then
  say "Restoring your originals"
  while IFS= read -r d; do
    [ -n "$d" ] && [ -d "$STATE/config-backup/$d" ] || continue
    run cp -a "$STATE/config-backup/$d" "$HOME/.config/$d"
    note "restored ~/.config/$d"
  done < "$STATE/config-backup/existed.txt"
  run touch "$STATE/config-backup/.restored"
else
  note "you had none of these configs before the lab ran, so there is nothing to restore"
fi

# ── done ──────────────────────────────────────────────────────────────────
say "Done — the desktops are all still installed"
echo
printf '%sYour sessions are untouched:%s\n' "$c_b" "$c_0"
for f in /usr/share/wayland-sessions/*.desktop /usr/share/xsessions/*.desktop; do
  [ -e "$f" ] || continue
  printf '  %s\n' "$(awk -F= '/^Name=/{print $2; exit}' "$f")"
done
echo
printf '%sEverything removed is in:%s %s\n' "$c_b" "$c_0" "$ARCHIVE"
note "unpack with:  tar -xzf $ARCHIVE -C ~"
note "still kept:   ~/.local/share/de-testlab/  (logs, package manifest, backups)"
note "to remove the packages too: $STATE/uninstall-desktops.sh"
echo
printf '%sHyprland, Sway and Awesome will start with their stock defaults now.%s\n' "$c_y" "$c_0"
printf '%sDrop any dotfiles you like into ~/.config — nothing of ours is in the way.%s\n' "$c_d" "$c_0"

#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  Desktop test lab REMOVER
#
#  Takes back out exactly what install-desktops.sh put in:
#    • every package that appeared during the install (recorded manifest)
#    • the COPR / Terra repos it added
#    • the session entries it created
#    • the Hyprland / Sway / Awesome configs it wrote (restoring any backup)
#  Your Plasma desktop and the KDE login manager are left untouched.
#
#  Usage:  ./uninstall-desktops.sh          # shows the plan, asks once
#          ./uninstall-desktops.sh -y       # no questions
#          ./uninstall-desktops.sh --dry-run
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

STATE="$HOME/.local/share/de-testlab"
KEEP_DM="plasmalogin.service"
OTHER_DMS="gdm lightdm sddm lxdm xdm ddm"     # same list the installer masks
ASSUME_YES=0; DRY=0
for a in "$@"; do
  case "$a" in
    -y|--yes) ASSUME_YES=1 ;;
    -n|--dry-run) DRY=1 ;;
    -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 0 ;;
  esac
done

c_b=$'\e[1m'; c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_d=$'\e[2m'; c_0=$'\e[0m'
say()  { printf '\n%s▶ %s%s\n' "$c_b$c_g" "$*" "$c_0"; }
warn() { printf '%s! %s%s\n' "$c_y" "$*" "$c_0"; }
note() { printf '%s  %s%s\n' "$c_d" "$*" "$c_0"; }
run()  { if [ "$DRY" = 1 ]; then printf '%s[dry-run]%s %s\n' "$c_d" "$c_0" "$*"; else "$@"; fi; }

[ -f "$STATE/manifest.pkgs" ] || { warn "no manifest at $STATE/manifest.pkgs — nothing recorded to remove"; exit 1; }

# never remove anything core, even if it somehow landed in the manifest
PROTECT='^(kernel|kernel-core|kernel-modules|systemd|glibc|dnf|rpm|dracut|grub2|plasma|plasmalogin|plasma-login-manager|sddm|kf5|kf6|kde|qt5|qt6|NetworkManager|mesa|nvidia|akmod|kmod|xorg-x11-drv|pipewire|wireplumber|dbus|polkit|bash|coreutils|sudo|python3)([-.]|$)'

mapfile -t WANT < <(grep -vE "$PROTECT" "$STATE/manifest.pkgs")
mapfile -t SKIP < <(grep -E  "$PROTECT" "$STATE/manifest.pkgs")
mapfile -t PRESENT < <(for p in "${WANT[@]}"; do rpm -q --qf '%{NAME}\n' "$p" >/dev/null 2>&1 && echo "$p"; done)

say "Removal plan"
printf '  %s%d%s packages to remove   %s(%d already gone, %d protected and kept)%s\n' \
  "$c_b" "${#PRESENT[@]}" "$c_0" "$c_d" "$(( ${#WANT[@]} - ${#PRESENT[@]} ))" "${#SKIP[@]}" "$c_0"
[ "${#SKIP[@]}" -gt 0 ] && note "kept: ${SKIP[*]}"
printf '  repos to drop:        %s\n' "$(tr '\n' ' ' < "$STATE/repos-added.txt" 2>/dev/null)"
printf '  session files to drop: %s\n' "$(tr '\n' ' ' < "$STATE/created-files.txt" 2>/dev/null | cut -c1-120)"
printf '  configs to remove:     %s\n' "$(tr '\n' ' ' < "$STATE/configs-deployed.txt" 2>/dev/null)"
[ -s "$STATE/config-backup/existed.txt" ] && printf '  configs to RESTORE:    %s\n' "$(tr '\n' ' ' < "$STATE/config-backup/existed.txt")"
echo

if [ "$ASSUME_YES" != 1 ] && [ "$DRY" != 1 ]; then
  read -r -p "Type ${c_b}yes${c_0} to go ahead: " ans
  [ "$ans" = "yes" ] || { echo "aborted, nothing changed"; exit 0; }
fi

sudo -v || exit 1
( while sudo -n true 2>/dev/null; do sleep 45; kill -0 "$$" 2>/dev/null || exit 0; done ) &
KEEPALIVE=$!
trap 'kill "$KEEPALIVE" 2>/dev/null' EXIT

# ── packages ──────────────────────────────────────────────────────────────
REMOVE_SKIP=""
case "$(dnf remove --help 2>&1)" in *--skip-unavailable*) REMOVE_SKIP="--skip-unavailable" ;; esac

if [ "${#PRESENT[@]}" -gt 0 ]; then
  say "Removing packages (this is the long part)"
  run sudo dnf -y remove "${PRESENT[@]}" $REMOVE_SKIP || warn "dnf remove reported problems — see output above"
  say "Cleaning up orphans"
  run sudo dnf -y autoremove || true
fi

# ── repos ─────────────────────────────────────────────────────────────────
say "Removing the repos the installer added"
while IFS= read -r line; do
  case "$line" in
    copr:*) run sudo dnf -y copr remove "${line#copr:}" >/dev/null 2>&1 || warn "copr ${line#copr:} already gone" ;;
    file:*) run sudo rm -f "${line#file:}" ;;
  esac
done < "$STATE/repos-added.txt" 2>/dev/null
run sudo dnf clean all >/dev/null 2>&1 || true

# ── session entries we created ────────────────────────────────────────────
say "Removing session entries and helper scripts we created"
while IFS= read -r f; do [ -n "$f" ] && run sudo rm -f "$f"; done < "$STATE/created-files.txt" 2>/dev/null

# ── configs: remove ours, restore any backup ──────────────────────────────
say "Restoring your ~/.config"
while IFS= read -r d; do
  [ -n "$d" ] || continue
  run rm -rf "$HOME/.config/$d"
  note "removed ~/.config/$d"
done < "$STATE/configs-deployed.txt" 2>/dev/null
if [ -s "$STATE/config-backup/existed.txt" ]; then
  while IFS= read -r d; do
    [ -d "$STATE/config-backup/$d" ] || continue
    run cp -a "$STATE/config-backup/$d" "$HOME/.config/$d"
    note "restored your original ~/.config/$d"
  done < "$STATE/config-backup/existed.txt"
fi
run rm -f "$HOME/.local/share/wallpapers/mocha-aurora.png"
if grep -q 'de-testlab PATH' "$HOME/.bashrc" 2>/dev/null; then
  run sed -i '/# de-testlab PATH/,+1d' "$HOME/.bashrc"
fi

# ── login manager back to a clean single state ────────────────────────────
say "Leaving the KDE Plasma login manager as the only display manager"
for dm in $OTHER_DMS; do run sudo systemctl unmask "$dm" >/dev/null 2>&1 || true; done

unit_file() {                  # unit_file <name.service> -> path on disk
  local u="$1" d
  for d in /etc/systemd/system /usr/local/lib/systemd/system \
           /usr/lib/systemd/system /lib/systemd/system; do
    [ -f "$d/$u" ] && { printf '%s' "$d/$u"; return 0; }
  done
  return 1
}
KEEP_PATH=$(unit_file "$KEEP_DM" || true)
if [ -z "$KEEP_PATH" ]; then
  # the Plasma login manager is gone (or never was there) — pick one that is
  for cand in $(basename "$(cat "$STATE/original-dm" 2>/dev/null)" 2>/dev/null) \
              sddm.service gdm.service lightdm.service; do
    case "$cand" in ""|/|display-manager.service) continue ;; esac
    if KEEP_PATH=$(unit_file "$cand"); then KEEP_DM="$cand"; break; fi
    KEEP_PATH=""
  done
fi
if [ -n "$KEEP_PATH" ]; then
  run sudo systemctl unmask "$KEEP_DM" >/dev/null 2>&1 || true
  run sudo ln -sf "$KEEP_PATH" /etc/systemd/system/display-manager.service
  run sudo systemctl enable --force "$KEEP_DM" >/dev/null 2>&1 \
    || run sudo systemctl enable "$KEEP_DM" >/dev/null 2>&1 || true
  note "display-manager -> $(readlink -f /etc/systemd/system/display-manager.service 2>/dev/null)"
else
  warn "no display manager unit left on disk — not touching display-manager.service"
  warn "install one before rebooting, e.g.: sudo dnf install plasma-login-manager kcm-plasmalogin"
fi

# ── done ──────────────────────────────────────────────────────────────────
say "Removal finished"
printf '%sSessions still available at login:%s\n' "$c_b" "$c_0"
for f in /usr/share/wayland-sessions/*.desktop /usr/share/xsessions/*.desktop; do
  [ -e "$f" ] || continue
  printf '  %s\n' "$(awk -F= '/^Name=/{print $2; exit}' "$f")"
done
echo
note "Records kept in $STATE (delete the folder when you are happy: rm -rf $STATE)"
note "If anything still looks off, 'sudo dnf history list' shows every transaction so you can 'sudo dnf history undo <id>'."

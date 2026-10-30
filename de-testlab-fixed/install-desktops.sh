#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  Desktop test lab installer  —  Fedora 44
#
#  Installs a set of desktops side by side, themed (Catppuccin Mocha) for
#  Hyprland / SwayFX / AwesomeWM, and keeps the KDE Plasma login manager as
#  the ONLY display manager (it lets you pick any session from its menu).
#
#  Which ones is up to the DESKTOPS list below, or the environment:
#      DESKTOPS="gnome cinnamon" bash install-desktops.sh
#
#  Asks for your sudo password ONCE, then runs unattended.
#
#    --dry-run        resolve the whole transaction and show what WOULD
#                     happen; installs nothing, touches no configs and
#                     leaves the login manager alone.  (It does still make
#                     sure the repos exist — nothing resolves without them.)
#    --upgrade-first  apply all pending Fedora updates before installing.
#                     OFF by default: run your own `sudo dnf upgrade` first
#                     if you want the versions kept consistent.
#
#  Everything it does is recorded in ~/.local/share/de-testlab/
#  so ./uninstall-desktops.sh can take it all back out.
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HOME/.local/share/de-testlab"
LOG="$STATE/install.log"
WALLPAPER="$HOME/.local/share/wallpapers/mocha-aurora.png"
KEEP_DM="plasmalogin.service"        # Fedora 44's Plasma login manager
KEEP_PATH=""                         # resolved by ensure_login_manager
PLM_PKGS="plasma-login-manager kcm-plasmalogin"
OTHER_DMS="gdm lightdm sddm lxdm xdm ddm"
DRY=0

# Desktops to install.  Delete a word to skip it, add one back to install it.
#   gnome, cinnamon - pull a very large pile of apps with them; removed by hand
#   deepin          - the treeland/DDE session black-screens back to the login
#                     manager on this machine, so it is out until that is fixed
DESKTOPS="${DESKTOPS:-hyprland swayfx awesome cosmic}"
want() { case " $DESKTOPS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

mkdir -p "$STATE"/{config-backup,logs} "$HOME/.local/bin" "$HOME/.local/share/wallpapers" "$HOME/Pictures/Screenshots"
touch "$STATE/created-files.txt"; : > "$STATE/steps.txt"
exec > >(tee -a "$LOG") 2>&1

c_b=$'\e[1m'; c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_d=$'\e[2m'; c_0=$'\e[0m'
say()  { printf '\n%s▶ %s%s\n' "$c_b$c_g" "$*" "$c_0"; }
warn() { printf '%s! %s%s\n' "$c_y" "$*" "$c_0"; }
err()  { printf '%s✘ %s%s\n' "$c_r" "$*" "$c_0"; }
note() { printf '%s  %s%s\n' "$c_d" "$*" "$c_0"; }
record() {                     # record <ledger-file> <line>  — append once only
  local f="$STATE/$1" line="$2"
  touch "$f"
  grep -qxF -- "$line" "$f" 2>/dev/null || printf '%s\n' "$line" >> "$f"
}

UPGRADE_FIRST=0
for a in "$@"; do
  case "$a" in
    --upgrade-first) UPGRADE_FIRST=1 ;;
    -n|--dry-run)    DRY=1 ;;
    -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 0 ;;
    *) printf 'unknown option: %s  (try --help)\n' "$a"; exit 2 ;;
  esac
done
[ "$DRY" = 1 ] && printf '\n%s*** DRY RUN — nothing will be installed or changed ***%s\n' "$c_b$c_y" "$c_0"

START=$(date +%s)
echo "=== de-testlab install started $(date -Is) ==="

# ── 1. sudo once, then keep the ticket warm ───────────────────────────────
say "Asking for your password once (nothing after this needs you)"
sudo -v || { err "no sudo, aborting"; exit 1; }
( while sudo -n true 2>/dev/null; do sleep 45; kill -0 "$$" 2>/dev/null || exit 0; done ) &
KEEPALIVE=$!
cleanup() { kill "$KEEPALIVE" 2>/dev/null; }
trap cleanup EXIT

# ── 2. record the "before" picture ────────────────────────────────────────
say "Recording system state (for clean removal)"
if [ -s "$STATE/baseline.pkgs" ]; then
  note "keeping the baseline from the first run ($(wc -l < "$STATE/baseline.pkgs") packages)"
else
  rpm -qa --qf '%{NAME}\n' | sort -u > "$STATE/baseline.pkgs"
  note "$(wc -l < "$STATE/baseline.pkgs") packages installed right now"
fi
# write-once as well: on a re-run the live symlink already points at our
# pick, so overwriting this would lose the real before-state.
if [ ! -s "$STATE/original-dm" ]; then
  if [ -e /etc/systemd/system/display-manager.service ]; then
    readlink -f /etc/systemd/system/display-manager.service 2>/dev/null > "$STATE/original-dm" || true
  fi
  note "login manager before we touched anything: $(cat "$STATE/original-dm" 2>/dev/null | grep . || echo none)"
else
  note "original login manager (recorded on the first run): $(cat "$STATE/original-dm")"
fi
sudo dnf history list --reverse 2>/dev/null | tail -1 | awk '{print $1}' > "$STATE/dnf-history-before" || true

# back up any config dirs we are about to touch
if [ ! -f "$STATE/config-backup/.done" ]; then
  for d in hypr sway awesome waybar wofi rofi kitty mako picom; do
    if [ -e "$HOME/.config/$d" ]; then
      cp -a "$HOME/.config/$d" "$STATE/config-backup/$d"
      echo "$d" >> "$STATE/config-backup/existed.txt"
      note "backed up ~/.config/$d"
    fi
  done
  touch "$STATE/config-backup/.done"
else
  note "your original ~/.config is already backed up from the first run"
fi

# ── 3. which skip flags does this dnf actually take, and where? ───────────
# dnf5 wants --skip-unavailable / --skip-broken AFTER the subcommand, and not
# every subcommand accepts them at all.  Getting that wrong killed every
# install step instantly on the first run, so ask dnf instead of guessing.
skip_flags() {                 # skip_flags <subcommand...>
  local help flags=""
  help=$(dnf "$@" --help 2>&1)
  case "$help" in *--skip-unavailable*) flags="$flags --skip-unavailable" ;; esac
  case "$help" in *--skip-broken*)      flags="$flags --skip-broken" ;; esac
  printf '%s' "$flags"
}
INSTALL_SKIP=$(skip_flags install)
GROUP_SKIP=$(skip_flags group install)
note "desktops: $DESKTOPS"
note "dnf install flags:${INSTALL_SKIP:- (none supported)}"
note "dnf group  flags:${GROUP_SKIP:- (none supported)}"

# ── 3a. keep exactly ONE display manager — and make sure it exists ────────
# A dangling /etc/systemd/system/display-manager.service with every other
# login manager masked means no graphical login on the next boot, so this
# never points at a unit that is not on disk.
unit_file() {                  # unit_file <name.service> -> path, ignoring masks
  local u="$1" d
  for d in /etc/systemd/system /usr/local/lib/systemd/system \
           /usr/lib/systemd/system /lib/systemd/system; do
    [ -f "$d/$u" ] && { printf '%s' "$d/$u"; return 0; }
  done
  return 1
}

ensure_login_manager() {
  KEEP_PATH=$(unit_file "$KEEP_DM" || true)
  if [ -z "$KEEP_PATH" ]; then
    warn "$KEEP_DM is not installed on this system"
    if [ "$DRY" = 1 ]; then
      note "(dry run) would install: $PLM_PKGS"
    else
      note "installing $PLM_PKGS"
      sudo dnf -y install $PLM_PKGS $INSTALL_SKIP 2>&1 | tail -3
      sudo systemctl daemon-reload >/dev/null 2>&1
      KEEP_PATH=$(unit_file "$KEEP_DM" || true)
    fi
  fi
  if [ -z "$KEEP_PATH" ]; then
    # fall back to a login manager that really is on disk, preferring the
    # one this machine booted with before we got involved
    local cand orig
    orig=$(basename "$(cat "$STATE/original-dm" 2>/dev/null)" 2>/dev/null)
    for cand in "$orig" sddm.service gdm.service lightdm.service ddm.service; do
      # "/" and the display-manager.service alias itself are not login managers
      case "$cand" in ""|/|display-manager.service) continue ;; esac
      if KEEP_PATH=$(unit_file "$cand"); then
        KEEP_DM="$cand"
        warn "falling back to the login manager you already have: $KEEP_DM"
        break
      fi
      KEEP_PATH=""
    done
  fi
  if [ -z "$KEEP_PATH" ]; then
    KEEP_DM=""
    err "no display manager unit found on disk — leaving your login setup ALONE"
    err "(nothing will be masked; you would otherwise end up with no login screen)"
  else
    note "login manager: $KEEP_DM  ->  $KEEP_PATH"
  fi
}

pin_login_manager() {
  [ "$DRY" = 1 ] && return 0
  [ -n "$KEEP_DM" ] || return 0            # refuse to break the login screen
  local dm
  for dm in $OTHER_DMS; do
    [ "$dm.service" = "$KEEP_DM" ] && continue
    sudo systemctl disable "$dm" >/dev/null 2>&1
    sudo systemctl mask    "$dm" >/dev/null 2>&1
  done
  sudo systemctl unmask "$KEEP_DM" >/dev/null 2>&1
  sudo ln -sf "$KEEP_PATH" /etc/systemd/system/display-manager.service
  sudo systemctl enable --force "$KEEP_DM" >/dev/null 2>&1 \
    || sudo systemctl enable "$KEEP_DM" >/dev/null 2>&1
}
say "Pinning the KDE Plasma login manager as the only display manager"
ensure_login_manager
pin_login_manager
[ -n "$KEEP_DM" ] && note "masked everything else: $OTHER_DMS"

# ── 3b. optional: apply all pending updates first ─────────────────────────
# Installing these desktops pulls ~175 of your pending updates in as
# dependencies, which leaves Plasma partially updated. --upgrade-first
# applies all of them instead, so versions stay consistent.
if [ "$UPGRADE_FIRST" = 1 ] && [ "$DRY" != 1 ]; then
  say "Applying all pending system updates first (--upgrade-first)"
  sudo dnf -y upgrade || warn "upgrade reported problems — continuing"
  note "a kernel or systemd update means you should reboot after all this"
fi

# ── 4. repos ──────────────────────────────────────────────────────────────
say "Adding package sources (Hyprland + SwayFX are not in Fedora 44)"
sudo dnf -y install dnf5-plugins >/dev/null 2>&1
add_copr() {
  if sudo dnf -y copr enable "$1" 2>&1 | tail -2; then
    record repos-added.txt "copr:$1"; note "COPR $1"
  else warn "could not enable COPR $1"; fi
}
want hyprland && add_copr byte0/hyprland
# liuyang/deepin-dde has no startdde, so its control center can never resolve.
# shrisha/deepin-treeland carries the whole modern DDE: startdde, dde-session,
# dde-shell, dde-control-center and the treeland Wayland compositor.
sudo dnf -y copr remove liuyang/deepin-dde >/dev/null 2>&1 \
  && note "dropped the incomplete liuyang/deepin-dde COPR"
# only when Deepin is actually wanted — otherwise a re-run would put back a
# COPR that was deliberately removed
if want deepin; then
  add_copr shrisha/deepin-treeland
else
  note "deepin not in DESKTOPS — leaving its COPR alone"
fi

TERRA_REPO=/etc/yum.repos.d/de-testlab-terra.repo
if [ ! -f "$TERRA_REPO" ]; then
  sudo tee "$TERRA_REPO" >/dev/null <<'REPOEOF'
[de-testlab-terra]
name=Terra (restricted to swayfx + nwg-look, added by de-testlab)
baseurl=https://repos.fyralabs.com/terra$releasever
enabled=0
gpgcheck=0
includepkgs=swayfx,nwg-look
metadata_expire=4h
REPOEOF
  if [ -s "$TERRA_REPO" ]; then
    record repos-added.txt "file:$TERRA_REPO"
    note "Terra repo (disabled by default, limited to swayfx + nwg-look)"
  else
    err "could not write $TERRA_REPO — SwayFX will be skipped"
  fi
fi

# ── 5. install ────────────────────────────────────────────────────────────
# only pass package names that actually exist, so one typo can't kill a step
avail() {
  local tmp; tmp=$(mktemp)
  dnf repoquery --qf '%{name}\n' "$@" 2>/dev/null | sort -u > "$tmp"
  local p out=() miss=()
  for p in "$@"; do
    if grep -qx -- "$p" "$tmp"; then out+=("$p"); else miss+=("$p"); fi
  done
  rm -f "$tmp"
  [ "${#miss[@]}" -gt 0 ] && note "no such package in the enabled repos, skipping: ${miss[*]}" >&2
  [ "${#out[@]}"  -gt 0 ] && printf '%s\n' "${out[@]}"
  return 0
}

pick_group() {                 # pick_group <id...> -> the first id dnf knows
  local g
  for g in "$@"; do
    if dnf group info "$g" >/dev/null 2>&1; then printf '%s' "$g"; return 0; fi
  done
  note "none of these package groups exist here: $*" >&2
  return 1
}

step() {                       # step <label> <dnf args...>
  local label="$1"; shift
  local flags="$INSTALL_SKIP" a n=0
  case " $* " in *" group "*) flags="$GROUP_SKIP" ;; esac

  # if avail() filtered every package out, there is nothing to ask dnf for —
  # and a bare `dnf install` is just an argument error
  for a in "$@"; do
    case "$a" in -*|install|group|remove|upgrade) ;; *) n=$((n+1)) ;; esac
  done
  if [ "$n" -eq 0 ]; then
    warn "$label: nothing available to install — skipped"
    echo "SKIP $label" >> "$STATE/steps.txt"; return 0
  fi

  if [ "$DRY" = 1 ]; then
    say "Dry run: $label"
    LC_ALL=C sudo dnf "$@" $flags --assumeno 2>&1 \
      | grep -viE '^(importing|last metadata|repositories loaded|updating and loading)' \
      | tail -18
    echo "DRY  $label" >> "$STATE/steps.txt"
    return 0
  fi

  say "Installing: $label"
  if sudo dnf -y "$@" $flags; then
    echo "OK   $label" >> "$STATE/steps.txt"; printf '%s✔ %s installed%s\n' "$c_g" "$label" "$c_0"
  else
    echo "FAIL $label" >> "$STATE/steps.txt"; err "$label FAILED (continuing)"
  fi
  pin_login_manager          # every DE drags in its own login manager
}

COMMON=$(avail waybar wofi rofi kitty mako swaybg swayidle swaylock wlogout \
                grim slurp maim scrot xclip cliphist wl-clipboard playerctl \
                pavucontrol brightnessctl network-manager-applet polkit-kde \
                xdg-desktop-portal-gtk jetbrains-mono-fonts fontawesome-6-free-fonts \
                fontawesome-6-brands-fonts papirus-icon-theme adw-gtk3-theme \
                lxappearance Thunar btop fastfetch jq socat ImageMagick \
                libnotify google-noto-sans-fonts \
                gnome-keyring gnome-keyring-pam libsecret seahorse | tr '\n' ' ')
step "shared tools, fonts and themes" install $COMMON

if want hyprland; then
  HYPR=$(avail hyprland hyprpaper hyprlock hypridle hyprpicker hyprcursor \
               xdg-desktop-portal-hyprland | tr '\n' ' ')
  step "Hyprland" install $HYPR
fi

want swayfx && step "SwayFX" --enablerepo=de-testlab-terra --nogpgcheck install swayfx

if want awesome; then
  # i3lock: what de-powermenu locks with under X11 (awesome)
  AWE=$(avail awesome lua-lgi picom feh xorg-x11-server-Xorg xorg-x11-xinit \
              xorg-x11-xinit-session xsetroot i3lock | tr '\n' ' ')
  step "AwesomeWM (+ Xorg, picom)" install $AWE
fi

want gnome    && step "GNOME"    group install gnome-desktop
want cinnamon && step "Cinnamon" group install cinnamon-desktop
want budgie   && step "Budgie"   group install budgie-desktop
want cosmic   && step "COSMIC"   group install \
                   "$(pick_group cosmic-desktop cosmic-desktop-environment || true)"

if want deepin; then
DEEPIN=$(avail startdde dde-session dde-shell dde-control-center treeland \
               dde-daemon dde-seatd dde-appearance deepin-application-manager \
               deepin-tray-loader deepin-service-manager dde-file-manager-bridge \
               deepin-desktop-base deepin-desktop-schemas deepin-icon-theme \
               deepin-gtk-theme deepin-terminal deepin-wallpapers \
               deepin-sound-theme deepin-account-faces | tr '\n' ' ')
step "Deepin / DDE (unofficial COPR, Wayland via treeland)" install $DEEPIN
fi

if [ "$DRY" != 1 ]; then
  sudo dnf -y --enablerepo=de-testlab-terra --nogpgcheck install nwg-look $INSTALL_SKIP >/dev/null 2>&1 || true
fi

if [ "$DRY" = 1 ]; then
  say "Dry run finished — nothing was installed, no config or login change made"
  sed 's/^/  /' "$STATE/steps.txt"
  note "re-run without --dry-run to actually apply it"
  exit 0
fi

# ── 5b. did the Deepin stack really land? ─────────────────────────────────
# deepin-control-center does not exist in this COPR; dde-control-center is
# the control centre the modern DDE stack ships, and it needs startdde +
# dde-session to be there too, which is what the other COPR was missing.
if want deepin; then
say "Checking the Deepin / DDE stack"
# ask rpm, not $PATH: some of these ship units and desktop files, not binaries
DDE_MISSING=""
for b in startdde dde-session dde-shell dde-control-center treeland; do
  if rpm -q "$b" >/dev/null 2>&1; then
    note "✔ $(rpm -q --qf '%{NAME}-%{VERSION}' "$b")"
  else
    DDE_MISSING="$DDE_MISSING $b"
  fi
done
if [ -n "$DDE_MISSING" ]; then
  warn "these Deepin packages did not install:$DDE_MISSING"
  note "see why with:  sudo dnf install$DDE_MISSING"
else
  printf '%s✔ full DDE stack present, control centre included%s\n' "$c_g" "$c_0"
fi
command -v dde-control-center >/dev/null 2>&1 \
  && note "control centre binary: $(command -v dde-control-center)"
if ls /usr/share/wayland-sessions /usr/share/xsessions 2>/dev/null \
     | grep -iqE 'deepin|treeland'; then
  ls /usr/share/wayland-sessions /usr/share/xsessions 2>/dev/null \
    | grep -iE 'deepin|treeland' | sed 's/^/     session entry: /'
else
  warn "no Deepin session entry found — it will not show up in the login menu"
fi
fi

# ── 6. what did we add? ───────────────────────────────────────────────────
say "Writing removal manifest"
rpm -qa --qf '%{NAME}\n' | sort -u > "$STATE/after.pkgs"
comm -13 "$STATE/baseline.pkgs" "$STATE/after.pkgs" > "$STATE/manifest.pkgs"
note "$(wc -l < "$STATE/manifest.pkgs") new packages recorded in $STATE/manifest.pkgs"

# ── 7. session entries for the login manager ──────────────────────────────
say "Making sure every desktop shows up in the login menu"
install_session() {            # install_session <dir> <file> <content>
  local dir="$1" file="$2" content="$3"
  if [ ! -f "$dir/$file" ]; then
    sudo mkdir -p "$dir"
    printf '%s' "$content" | sudo tee "$dir/$file" >/dev/null
    record created-files.txt "$dir/$file"
    note "created $dir/$file"
  fi
}
if command -v sway >/dev/null; then
  install_session /usr/share/wayland-sessions swayfx.desktop \
'[Desktop Entry]
Name=SwayFX
Comment=Sway with blur, shadows and rounded corners
Exec=sway
Type=Application
DesktopNames=sway
'
fi
if command -v Hyprland >/dev/null || command -v hyprland >/dev/null; then
  HYPRBIN=$(command -v Hyprland || command -v hyprland)
  install_session /usr/share/wayland-sessions hyprland.desktop \
"[Desktop Entry]
Name=Hyprland
Comment=Dynamic tiling Wayland compositor
Exec=$HYPRBIN
Type=Application
DesktopNames=Hyprland
"
fi

# ── 8. drop in the themed configs ─────────────────────────────────────────
say "Installing themed configs for Hyprland, SwayFX and AwesomeWM"
rm -rf "$STATE/configs"                       # plain `cp -a` would nest it on a re-run
cp -a "$SRC/configs" "$STATE/configs"         # keep a copy for uninstall/restore
install -m644 "$SRC/configs/wallpapers/mocha-aurora.png" "$WALLPAPER"

KBLAYOUT=$(localectl status 2>/dev/null | awk '/X11 Layout/{print $3}'); KBLAYOUT=${KBLAYOUT:-us}
KBMODEL=$(localectl  status 2>/dev/null | awk '/X11 Model/{print $3}');  KBMODEL=${KBMODEL:-pc105}
note "keyboard: layout=$KBLAYOUT model=$KBMODEL   wallpaper: $WALLPAPER"

deploy() {                     # deploy <subdir>
  local d="$1"
  rm -rf "$HOME/.config/$d"
  mkdir -p "$HOME/.config/$d"
  cp -a "$SRC/configs/$d/." "$HOME/.config/$d/"
  find "$HOME/.config/$d" -type f -exec sed -i \
    -e "s|@WALLPAPER@|$WALLPAPER|g" \
    -e "s|@KBLAYOUT@|$KBLAYOUT|g" \
    -e "s|@KBMODEL@|$KBMODEL|g" {} +
  record configs-deployed.txt "$d"
}
for d in hypr sway awesome waybar wofi rofi kitty mako picom; do deploy "$d"; done

for f in "$SRC"/configs/bin/*; do
  install -m755 "$f" "$HOME/.local/bin/$(basename "$f")"
  record created-files.txt "$HOME/.local/bin/$(basename "$f")"
done

# ── one secret store for every session ────────────────────────────────────
# Plasma keeps passwords in KWallet; the other sessions have no secret store
# at all, so anything that saves a credential loses it the moment you switch
# desktop.  gnome-keyring answers the same org.freedesktop.secrets D-Bus name
# everywhere, so one store serves all of them.  Hyprland, SwayFX and Awesome
# start it from their own configs; this autostart entry covers the desktops
# that read XDG autostart, Plasma included.  For Plasma to actually use it,
# KWallet has to be switched off (System Settings -> KDE Wallet), otherwise
# both daemons claim the same D-Bus name and you are back to two stores.
if command -v gnome-keyring-daemon >/dev/null; then
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/de-testlab-keyring.desktop" <<'DESKEOF'
[Desktop Entry]
Type=Application
Name=Secret storage (gnome-keyring)
Comment=Provides org.freedesktop.secrets where the session has no keyring of its own
Exec=/usr/bin/gnome-keyring-daemon --start --components=secrets,ssh
X-GNOME-Autostart-Phase=Initialization
NoDisplay=true
DESKEOF
  record created-files.txt "$HOME/.config/autostart/de-testlab-keyring.desktop"
  note "secret storage: gnome-keyring, autostarted in every session"
  if [ -r "$HOME/.config/kwalletrc" ] && grep -qiE '^\s*Enabled\s*=\s*false' "$HOME/.config/kwalletrc"; then
    note "KWallet is disabled — gnome-keyring is your only secret store"
  else
    warn "KWallet still looks enabled: Plasma will keep using it, not gnome-keyring"
    note "turn it off in System Settings -> KDE Wallet to have one store everywhere"
  fi
else
  warn "gnome-keyring did not install — saved passwords will not persist outside Plasma"
fi
# swaylock reads ~/.config/swaylock/config
mkdir -p "$HOME/.config/swaylock"
sed "s|@WALLPAPER@|$WALLPAPER|g" "$SRC/configs/sway/swaylock.conf" > "$HOME/.config/swaylock/config"
record configs-deployed.txt swaylock

# make sure ~/.local/bin is on PATH for graphical sessions
if ! printf '%s' "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  grep -q 'de-testlab PATH' "$HOME/.bashrc" 2>/dev/null || \
    printf '\n# de-testlab PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.bashrc"
fi

# ── 9. validate ───────────────────────────────────────────────────────────
say "Validating configs"
if command -v sway >/dev/null; then
  sway --validate -c "$HOME/.config/sway/config" 2>&1 | grep -vi "^\[" | head -10
  sway --validate -c "$HOME/.config/sway/config" >/dev/null 2>&1 \
    && printf '%s✔ sway config OK%s\n' "$c_g" "$c_0" || warn "sway reported config warnings (see above)"
fi
if command -v awesome >/dev/null; then
  if awesome -k -c "$HOME/.config/awesome/rc.lua" >/dev/null 2>"$STATE/logs/awesome-check.log"; then
    printf '%s✔ awesome rc.lua OK%s\n' "$c_g" "$c_0"
  else warn "awesome rc.lua check failed: $(tail -3 "$STATE/logs/awesome-check.log")"; fi
fi
if command -v hyprland >/dev/null || command -v Hyprland >/dev/null; then
  printf '%s✔ hyprland installed%s\n' "$c_g" "$c_0"
fi
python3 -c "import json,sys,re
for f in ['$HOME/.config/waybar/config.jsonc','$HOME/.config/waybar/config-sway.jsonc']:
    s=re.sub(r'(?m)^\s*//.*$','',open(f).read()); json.loads(s)
print('✔ waybar configs are valid JSON')" 2>/dev/null || warn "check waybar json"

# ── 10. the uninstaller lives next to the state ───────────────────────────
cp "$SRC/uninstall-desktops.sh" "$STATE/uninstall-desktops.sh" 2>/dev/null && chmod +x "$STATE/uninstall-desktops.sh"

# ── 11. report ────────────────────────────────────────────────────────────
pin_login_manager
say "Done in $(( ($(date +%s) - START) / 60 )) min $(( ($(date +%s) - START) % 60 ))s"
echo
printf '%sInstall results%s\n' "$c_b" "$c_0"; sed 's/^/  /' "$STATE/steps.txt"
echo
DM_LINK=/etc/systemd/system/display-manager.service
DM_TARGET=$(readlink -f "$DM_LINK" 2>/dev/null)
if [ -n "$DM_TARGET" ] && [ -f "$DM_TARGET" ]; then
  printf '%sLogin manager%s  %s %s(%s)%s\n' "$c_b" "$c_0" "$DM_TARGET" \
    "$c_d" "$(systemctl is-enabled "$(basename "$DM_TARGET")" 2>/dev/null || echo 'enable state unknown')" "$c_0"
else
  err "display-manager.service does not point at a real unit — you would get NO login screen"
  err "fix it before rebooting, e.g.:  sudo dnf install $PLM_PKGS && sudo systemctl enable --force $KEEP_DM"
  err "or go back to what you had:     sudo systemctl unmask sddm && sudo systemctl enable --force sddm"
fi
printf '%sSessions you can now pick at login:%s\n' "$c_b" "$c_0"
for f in /usr/share/wayland-sessions/*.desktop /usr/share/xsessions/*.desktop; do
  [ -e "$f" ] || continue
  printf '  %-22s %s\n' "$(awk -F= '/^Name=/{print $2; exit}' "$f")" "$(basename "$(dirname "$f")")"
done
echo
printf '%sTo remove everything:%s  %s\n' "$c_b" "$c_0" "$STATE/uninstall-desktops.sh"
printf '%sLog:%s %s\n' "$c_d" "$c_0" "$LOG"
echo
printf '%sNext: log out, then pick a session from the gear/session menu in the Plasma login screen.%s\n' "$c_y" "$c_0"
printf '%sHyprland / SwayFX / Awesome cheat sheet: Super+Return terminal, Super+D launcher, Super+Q close,%s\n' "$c_d" "$c_0"
printf '%sSuper+Shift+E power menu, Print screenshot, Super+/ (awesome) or Super+Shift+C (hypr) help/reload.%s\n' "$c_d" "$c_0"

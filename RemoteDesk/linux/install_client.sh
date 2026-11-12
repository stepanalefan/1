#!/usr/bin/env bash
# RemoteDesk client for Linux: Ubuntu 24.04 / 26.04, Raspberry Pi 5 (arm64) and x86-64 PCs.
#
#   ./install_client.sh               install or update for the current user (asks for sudo once)
#   ./install_client.sh --server      also add "RemoteDesk Server" (window app) to the menu
#   ./install_client.sh --uninstall   remove it
#
# Installs to ~/.local/share/remotedesk, adds "RemoteDesk" to the app menu and the
# command "remotedesk". Settings stay in ~/.remotedesk.json.
set -euo pipefail

WITH_SERVER=0
UNINSTALL=0
for a in "$@"; do
    case "$a" in
        --server) WITH_SERVER=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Unknown option: $a"; exit 1 ;;
    esac
done

[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user (not with sudo); it asks for sudo when needed."; exit 1; }

BASE="$HOME/.local/share/remotedesk"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$UNINSTALL" -eq 1 ]; then
    rm -rf "$BASE" "$BIN/remotedesk" "$BIN/remotedesk-server-gui" \
           "$APPS/remotedesk.desktop" "$APPS/remotedesk-server.desktop" \
           "$ICONS/remotedesk.png" "$ICONS/remotedesk-server.png" \
           "$HOME/.config/autostart/remotedesk.desktop"
    echo "RemoteDesk removed (settings kept in ~/.remotedesk.json)."
    exit 0
fi

[ -f "$SRC/remote_desk/app.py" ] || { echo "Run this script from the RemoteDesk/linux folder."; exit 1; }

echo "==> Installing system packages (sudo)"
# PyQt5 has no pip builds for arm64, so the GUI toolkit (and the other binary
# packages) come from Ubuntu; pip only adds what Ubuntu lacks or ships too old.
PKGS="python3 python3-venv python3-pip python3-pyqt5 python3-cryptography python3-numpy python3-pil \
python3-cffi python3-xlib libpulse0"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS >/dev/null
# optional system packages (newer Ubuntu has them; pip covers the rest)
for p in python3-pynput python3-mss python3-av; do
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$p" >/dev/null 2>&1 || true
done

echo "==> Creating Python environment in $BASE/venv"
mkdir -p "$BASE"
# Ubuntu's own Python (not one from /usr/local or pyenv): the apt packages above belong to it.
# --system-site-packages: use Ubuntu's PyQt5, numpy, cryptography ...
SYSPY="${REMOTEDESK_PYTHON:-/usr/bin/python3}"   # override only for testing
if [ -d "$BASE/venv" ] && ! grep -q "^home = /usr/bin$" "$BASE/venv/pyvenv.cfg" 2>/dev/null; then
    rm -rf "$BASE/venv"                      # made with another Python: rebuild
fi
[ -x "$BASE/venv/bin/python" ] || "$SYSPY" -m venv --system-site-packages "$BASE/venv"
PIP="$BASE/venv/bin/python -m pip"
$PIP install -q --upgrade pip >/dev/null 2>&1 || true
# mss: not packaged on 24.04; av: 24.04 ships 11.x, take the current one; soundcard: not packaged
$PIP install -q "mss>=9" "av>=12" "soundcard>=0.4" \
    || { echo "    (pip failed for some optional packages — video/sound may be unavailable)"; }
$BASE/venv/bin/python -c "import pynput" 2>/dev/null || $PIP install -q "pynput>=1.7"

echo "==> Copying program"
rm -rf "$BASE/app"
mkdir -p "$BASE/app/remote_desk" "$BASE/app/assets"
cp "$SRC"/remote_desk/*.py "$BASE/app/remote_desk/"
cp "$SRC"/assets/*.png "$SRC"/assets/*.ico "$BASE/app/assets/" 2>/dev/null || true
cp "$SRC/RemoteDesk.py" "$SRC/RemoteDeskServer.py" "$BASE/app/"

echo "==> Adding menu entries and the 'remotedesk' command"
mkdir -p "$BIN" "$APPS" "$ICONS"
cat > "$BIN/remotedesk" <<LAUNCH
#!/bin/sh
cd "$BASE/app" && exec "$BASE/venv/bin/python" RemoteDesk.py "\$@"
LAUNCH
chmod +x "$BIN/remotedesk"
cp "$SRC/assets/RemoteDesk.png" "$ICONS/remotedesk.png"
cat > "$APPS/remotedesk.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=RemoteDesk
Comment=Remote desktop: connect to other computers or share this one
Exec=$BIN/remotedesk
Icon=remotedesk
Terminal=false
Categories=Network;RemoteAccess;
StartupWMClass=RemoteDesk
DESK

if [ "$WITH_SERVER" -eq 1 ]; then
    cat > "$BIN/remotedesk-server-gui" <<LAUNCH
#!/bin/sh
cd "$BASE/app" && exec "$BASE/venv/bin/python" RemoteDeskServer.py "\$@"
LAUNCH
    chmod +x "$BIN/remotedesk-server-gui"
    cp "$SRC/assets/RemoteDeskServer.png" "$ICONS/remotedesk-server.png"
    cat > "$APPS/remotedesk-server.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=RemoteDesk Server
Comment=Relay server for RemoteDesk desk IDs (window version)
Exec=$BIN/remotedesk-server-gui
Icon=remotedesk-server
Terminal=false
Categories=Network;
DESK
fi
update-desktop-database "$APPS" >/dev/null 2>&1 || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "==> Checking"
"$BASE/venv/bin/python" - <<'CHECK'
# find_spec, not import: pynput and soundcard connect to the desktop / sound server when
# imported, which fails in a terminal without a desktop although they are installed fine
import importlib.util
for mod, what in [("PyQt5.QtWidgets", "window"), ("cryptography", "encryption"), ("mss", "screen capture"),
                  ("pynput", "mouse/keyboard control"), ("av", "video stream + sound"), ("soundcard", "sound")]:
    try:
        ok = importlib.util.find_spec(mod) is not None
    except Exception:
        ok = False
    print(f"    {'ok     ' if ok else 'MISSING'}  {what}")
CHECK

echo
echo "RemoteDesk is installed: open it from the app menu, or run: remotedesk"
case "${XDG_SESSION_TYPE:-}" in
    wayland)
        echo
        echo "Note: this desktop uses Wayland. You can control other computers from here, but"
        echo "others cannot control this one. On Ubuntu 24.04 choose \"Ubuntu on Xorg\" on the login"
        echo "screen (gear icon) to share this computer; Ubuntu 25.10+ has no Xorg session." ;;
esac
case ":$PATH:" in *":$BIN:"*) ;; *) echo "(Log out and in again so the 'remotedesk' command is found.)" ;; esac

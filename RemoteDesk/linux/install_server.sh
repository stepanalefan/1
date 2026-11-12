#!/usr/bin/env bash
# RemoteDesk Server for Linux (Raspberry Pi 5 / Ubuntu 26.04, also other Ubuntu/Debian).
#
#   sudo ./install_server.sh                 install or update, port 7070
#   sudo ./install_server.sh --port 7000     another port
#   sudo ./install_server.sh --gui           also install the window app "RemoteDesk Server"
#                                            (automatic when this computer boots into a desktop)
#   sudo ./install_server.sh --no-gui        never install the window app
#   sudo ./install_server.sh --uninstall     remove the service (keeps the desk list)
#
# Runs as the systemd service "remotedesk-server" under its own user, starts at
# boot, restarts on failure. Desk IDs: /var/lib/remotedesk/desks.json.
# Manage desks with:  sudo remotedesk-admin list | sessions | set-id OLD NEW | delete ID
set -euo pipefail

PORT=7070
UNINSTALL=0
GUI=auto
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        --gui) GUI=yes; shift ;;
        --no-gui) GUI=no; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo: sudo $0 $*"; exit 1; }

APP=/opt/remotedesk
DATA=/var/lib/remotedesk
UNIT=/etc/systemd/system/remotedesk-server.service
SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$UNINSTALL" -eq 1 ]; then
    systemctl disable --now remotedesk-server 2>/dev/null || true
    rm -f "$UNIT" /usr/local/bin/remotedesk-admin /usr/local/bin/remotedesk-server-gui \
          /usr/share/applications/remotedesk-server.desktop \
          /usr/share/icons/hicolor/256x256/apps/remotedesk-server.png
    systemctl daemon-reload
    rm -rf "$APP/server"
    echo "RemoteDesk Server removed. The desk list is kept in $DATA (delete it by hand if you want)."
    exit 0
fi

[ -f "$SRC/remote_desk/relay.py" ] || { echo "Run this script from the RemoteDesk/linux folder."; exit 1; }

echo "==> Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-cryptography >/dev/null

echo "==> Creating user 'remotedesk'"
id remotedesk >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin remotedesk
install -d -o remotedesk -g remotedesk -m 750 "$DATA"
# keep the desk list of an earlier run of the server GUI / manual server, if any
if [ ! -f "$DATA/desks.json" ] && [ -n "${SUDO_USER:-}" ]; then
    OLD="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.remotedesk_server_desks.json"
    [ -f "$OLD" ] && install -o remotedesk -g remotedesk -m 640 "$OLD" "$DATA/desks.json" && echo "    imported $OLD"
fi

echo "==> Copying program to $APP/server"
install -d -m 755 "$APP/server/remote_desk"
install -m 644 "$SRC"/remote_desk/*.py "$APP/server/remote_desk/"
install -d -m 755 "$APP/server/assets"
install -m 644 "$SRC"/assets/*.png "$APP/server/assets/" 2>/dev/null || true

echo "==> Installing service (port $PORT)"
cat > "$UNIT" <<UNIT
[Unit]
Description=RemoteDesk Server (relay for desk IDs)
After=network-online.target
Wants=network-online.target

[Service]
User=remotedesk
Group=remotedesk
WorkingDirectory=$APP/server
ExecStart=/usr/bin/python3 -m remote_desk.relay --port $PORT --desks $DATA/desks.json --admin-socket /run/remotedesk/admin.sock
Restart=always
RestartSec=3
RuntimeDirectory=remotedesk
RuntimeDirectoryMode=0750
Environment=PYTHONUNBUFFERED=1
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ReadWritePaths=$DATA
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true

[Install]
WantedBy=multi-user.target
UNIT

cat > /usr/local/bin/remotedesk-admin <<ADMIN
#!/bin/sh
PYTHONPATH=$APP/server exec /usr/bin/python3 -m remote_desk.admin "\$@"
ADMIN
chmod 755 /usr/local/bin/remotedesk-admin
# let the installing user run remotedesk-admin without sudo (after logging in again)
[ -n "${SUDO_USER:-}" ] && usermod -aG remotedesk "$SUDO_USER" || true

if [ "$GUI" = auto ]; then
    [ "$(systemctl get-default 2>/dev/null)" = graphical.target ] && GUI=yes || GUI=no
fi
if [ "$GUI" = yes ]; then
    echo "==> Installing the window app 'RemoteDesk Server'"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pyqt5 pkexec >/dev/null 2>&1 \
        || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pyqt5 policykit-1 >/dev/null
    cat > /usr/local/bin/remotedesk-server-gui <<LAUNCH
#!/bin/sh
cd $APP/server && PYTHONPATH=$APP/server exec /usr/bin/python3 -m remote_desk.server_gui "\$@"
LAUNCH
    chmod 755 /usr/local/bin/remotedesk-server-gui
    install -D -m 644 "$SRC/assets/RemoteDeskServer.png" /usr/share/icons/hicolor/256x256/apps/remotedesk-server.png
    cat > /usr/share/applications/remotedesk-server.desktop <<DESK
[Desktop Entry]
Type=Application
Name=RemoteDesk Server
Comment=Manage the RemoteDesk Server: desks, IDs, sessions, log
Exec=/usr/local/bin/remotedesk-server-gui
Icon=remotedesk-server
Terminal=false
Categories=Network;System;
DESK
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
    gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable remotedesk-server >/dev/null 2>&1
systemctl restart remotedesk-server

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" comment "RemoteDesk Server" >/dev/null && echo "==> Firewall: opened port $PORT/tcp"
fi

sleep 2
if systemctl is-active --quiet remotedesk-server; then
    echo
    echo "RemoteDesk Server is running on port $PORT."
    echo "Clients connect to (Settings -> Server):"
    for ip in $(hostname -I); do
        case "$ip" in fe80:*) ;; *:*) echo "    [$ip]:$PORT" ;; *) echo "    $ip:$PORT" ;; esac
    done
    echo
    echo "From the internet: forward TCP port $PORT on your router to this Pi and use your public IP."
    echo "Status / log:   systemctl status remotedesk-server   |   journalctl -u remotedesk-server -f"
    echo "Desks:          sudo remotedesk-admin list"
    [ "$GUI" = yes ] && echo "Window app:     'RemoteDesk Server' in the app menu (log out and in once first)"
else
    echo "The service did not start. See: journalctl -u remotedesk-server -n 50"
    exit 1
fi

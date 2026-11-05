# RemoteDesk

Self-hosted remote desktop with two applications:

| App | Who runs it | What it does |
|---|---|---|
| **RemoteDesk Server** (`RemoteDeskServer.py` / `.exe`) | one computer reachable by everyone (VPS, office server, home PC with port forwarding) | hands out 9-digit desk IDs and forwards encrypted traffic between desks |
| **RemoteDesk** (`RemoteDesk.py` / `.exe`) | every end user | shows "This Desk" (ID + password) and connects to other desks |

## Quick start (Windows)
1. Open this `RemoteDesk` folder in PyCharm / a terminal and create a venv:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. On the server computer: `python RemoteDeskServer.py` → **Start server**.
   The window shows the address clients must use (e.g. `192.168.1.10:7070`).
   For internet use: open TCP port 7070 in Windows Firewall and forward it on the
   router (or use a VPS), then give clients the public IP / domain.
3. On each user's computer: `python RemoteDesk.py` → **Settings** → *Server* = that address.
   The dot in the status bar turns green and **Your address** shows the desk ID.
4. To connect: type the other desk's ID in the top bar → **Connect** → enter its password →
   the other user clicks **Accept**.

### Build .exe files
Run `build_windows.bat` → `dist\RemoteDesk.exe` and `dist\RemoteDeskServer.exe`
(single files, no Python needed on target machines).

### Icons
`assets/RemoteDesk.ico` (client, red) and `assets/RemoteDeskServer.ico` (server, blue)
contain 16–256 px sizes. They are used for the .exe files, windows, taskbar and tray.
To change them, edit and run `python make_icons.py`, or replace the .ico files with your own.

## Android app (viewer)
Control your computers from an Android phone or tablet (Android 7+).

**Get the APK:** the latest build is committed to the branch as
`RemoteDesk/dist/RemoteDesk-android.apk` (open it on GitHub → *Download raw file*).
It is also attached to every build run on GitHub:
repository → **Actions** → *Android APK* → latest run → **Artifacts** → `RemoteDesk-android`
(a .zip containing `RemoteDesk-android.apk`). Copy it to the phone and open it; allow
"Install unknown apps" for your browser / file manager when asked.

Or build it yourself: open `RemoteDesk/android` in Android Studio → Run, or
`cd android && ./gradlew assembleDebug` (needs the Android SDK).

**Use:** Settings → Server = the same address as on the PCs, then enter a desk ID → Connect →
password. Direct addresses (`192.168.1.20`) work on the same Wi-Fi without a server.

| Gesture | Touchpad mode (default) | Direct touch mode |
|---|---|---|
| One-finger drag | move the pointer | drag / select |
| Tap | left click at the pointer | click at that spot |
| Double tap + drag | drag / select | — |
| Long press / two-finger tap | right click | right click (long press) |
| Two-finger drag | scroll | scroll |
| Pinch | zoom (move to pan) | zoom (move to pan) |

Toolbar: **Keyboard**, **Keys** (Ctrl/Alt/Shift/Win stay pressed until the next key, Esc, Tab,
arrows, Home/End, PgUp/PgDn, F1–F12, Ctrl+Alt+Del), mouse mode, **More** (fit to screen,
monitor, quality, send clipboard, message, fullscreen, disconnect). ▲ hides the toolbar.

Note: the APK is signed with a debug key that changes between CI runs, so to install a newer
build you may need to uninstall the old one first.

## Client features
- Desk ID + random password (show / copy / regenerate), recent sessions as tiles
  (click to connect, right-click to rename / remove)
- Accept prompt with "allow control" choice and auto-dismiss; red banner while a
  session is active (click to end); runs in the tray so the desk stays reachable
- Viewer: mouse / keyboard / wheel, Ctrl+Alt+Del, fullscreen, monitor selection,
  quality modes, clipboard, chat, file transfer (button or drag & drop →
  `Downloads\RemoteDesk`)
- Direct LAN mode without a server: connect to `ip:7071`
- Settings: server address, direct port, unattended access, view-only

## Server features
- Start/stop, port, autostart, runs in the tray
- Shows the addresses clients should use
- Live stats: desks online, active sessions, total sessions, traffic
- Tables of online desks and active sessions (with **Disconnect**), event log
- Headless mode for Linux servers: `python -m remote_desk.relay --port 7070`

## Security
End-to-end encrypted (X25519 key exchange confirmed with the password, AES-256-GCM).
The server only forwards ciphertext and never sees passwords or screen contents.
Wrong passwords are throttled. Use strong passwords, especially with unattended access.

## Limitations
- JPEG tiles instead of a video codec (more bandwidth for full-screen video)
- No audio, printing, or peer-to-peer hole punching (ID sessions always go through the server)
- Linux: X11 only (not Wayland). macOS: grant Screen Recording + Accessibility.
- Can't control the Windows login screen / UAC prompts (would need a Windows service)

Settings: `%USERPROFILE%\.remotedesk.json` (client), `%USERPROFILE%\.remotedesk_server.json` (server).

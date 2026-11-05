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

## Without a server: Tailscale
[Tailscale](https://tailscale.com/download) (free for personal use) connects your devices
through NAT and firewalls with an encrypted tunnel, so no RemoteDesk Server is needed.

1. Install Tailscale on every computer and on the phone, log in with the **same account**.
2. Start RemoteDesk on the computers (direct port 7071 must be on — the default).
   On Windows, allow RemoteDesk / Python when the firewall asks.
3. Desktop: the **Tailscale devices** tab lists your devices; "RemoteDesk ready" ones can be
   clicked to connect. "This Desk" shows your Tailscale address.
4. Android: type the computer's Tailscale name (e.g. `office-pc`) or 100.x address. To see a
   device list in the app, add a read-only key in Settings → *Tailscale API key*
   (admin console → Settings → Keys; an OAuth client with scope `devices:core:read` never expires,
   an API access token expires after at most 90 days).

The RemoteDesk password and accept prompt still apply; Tailscale only provides the network.

## IPv6
Server and desks listen on IPv4 and IPv6. Write IPv6 addresses in brackets with a port:
`[2001:db8::10]:7070`. Most routers block incoming IPv6 by default — allow the port there.

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

### Share the phone (control Android from a PC)
Main screen → **Share this phone**:
1. **Setup** (once): enable *Remote control* (Settings → Accessibility → Installed apps →
   RemoteDesk), allow notifications, and allow running in the background.
2. **Start sharing**. The phone shows its address: the desk ID (with a server), its Tailscale
   address, or its Wi-Fi address — and a password.
3. On the PC, connect to that ID / address with the password. The phone asks **Allow?**, then
   Android asks to start recording — choose **Entire screen**.

| On the PC | On the phone |
|---|---|
| click / drag | tap / swipe (hold > 0.5 s = long press) |
| right click | Back |
| middle click | Home |
| mouse wheel | scroll |
| typing, Backspace, Enter, arrows | edits the focused text field |
| Ctrl+C / V / X / A | copy / paste / cut / select all |
| Esc / Win / F1 / F2 | Back / Home / recent apps / notifications |

A red banner and a notification show while a session runs; tap either to end it. The screen
stays on during a session. Limits: Android needs the "start recording" confirmation for every
session (so nobody can connect unnoticed), a locked phone shows only the lock screen, and some
apps (banking, DRM video) appear black.

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

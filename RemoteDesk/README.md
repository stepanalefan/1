# RemoteDesk

Self-hosted remote desktop for Windows, Linux (incl. Raspberry Pi 5) and Android.

| App | Who runs it | What it does |
|---|---|---|
| **RemoteDesk Server** (`RemoteDeskServer.py` / `.exe`, Linux service) | one computer reachable by everyone (VPS, office server, Raspberry Pi, home PC with port forwarding) | assigns permanent 9-digit desk IDs and forwards encrypted traffic between desks |
| **RemoteDesk** (`RemoteDesk.py` / `.exe`, Linux menu entry) | every computer | shows "This Desk" (ID + password), connects to other desks, can be controlled |
| **RemoteDesk for Android** (`dist/RemoteDesk-android.apk`) | phones / tablets | controls computers; can share the phone |

All parts work together. Update **both ends** of a connection (and the server) to the same version:
older apps connect, but miss newer features (an old desk shows new chat messages as raw text).

## Quick start (Windows)
1. Open this `RemoteDesk` folder in PyCharm / a terminal and create a venv:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   (`av`, `soundcard` and `numpy` are optional: without them there is no video mode and no sound.)
2. On the server computer: `python RemoteDeskServer.py` → **Start server**.
   The window shows the address clients must use (e.g. `192.168.1.10:7070`).
   For internet use: open TCP port 7070 in Windows Firewall and forward it on the
   router (or use a VPS), then give clients the public IP / domain.
3. On each computer: `python RemoteDesk.py` → **Settings** → *Server* = that address.
   The dot in the status bar turns green and **Your address** shows the desk ID.
4. To connect: type the other desk's ID in the top bar → **Connect** → enter its password →
   the other user clicks **Accept**.

### Build .exe files
Run `build_windows.bat` → `dist\RemoteDesk\RemoteDesk.exe` and
`dist\RemoteDeskServer\RemoteDeskServer.exe` (folders, also zipped in `dist\`; no Python
needed on target machines — copy the whole folder).

### Smart App Control / SmartScreen
Windows 11 Smart App Control blocks programs that are not signed with a trusted
code-signing certificate. To ship RemoteDesk to other PCs, sign it:

1. Get a code-signing certificate from a trusted authority, for example
   **Azure Trusted Signing** (Microsoft, monthly fee; identity check) or a classic
   certificate (Certum, Sectigo, DigiCert …; on a USB token or cloud HSM).
   Self-signed certificates do **not** work.
2. Create `sign_config.bat` next to `build_windows.bat` (it is not committed), e.g.
   `set SIGN_CERT_SHA1=<thumbprint>` — or for Azure `set SIGN_AZURE_DLIB=…` and
   `set SIGN_AZURE_METADATA=…` (see `sign_windows.ps1`).
3. Run `build_windows.bat`: every .exe/.dll/.pyd is signed and checked.

Without a certificate: run from source (`python RemoteDesk.py`), or on your own PCs turn
Smart App Control off (Windows Security → App & browser control; it cannot be turned back
on without resetting Windows).

### Icons
`assets/RemoteDesk.ico` (client, red) and `assets/RemoteDeskServer.ico` (server, blue)
contain 16–256 px sizes. They are used for the .exe files, windows, taskbar and tray, and
for the Android launcher icon. To change them, edit and run `python make_icons.py`, or
replace the .ico files with your own.

## Linux: Raspberry Pi 5 and Ubuntu 24.04 / 26.04
Both installers are in `linux/` and work on Raspberry Pi 5 (arm64) and x86-64 PCs.

**Server** (runs in the background, starts at boot, no screen needed):
```
cd RemoteDesk/linux
sudo ./install_server.sh                 # port 7070; --port N, --gui / --no-gui, --uninstall
sudo remotedesk-admin list               # desks: ID, computer name, online, IP, last seen
sudo remotedesk-admin set-id OLD NEW     # change a desk's ID (an online desk is told at once)
sudo remotedesk-admin delete ID          # forget a desk
sudo remotedesk-admin sessions           # active connections (disconnect SID ends one)
journalctl -u remotedesk-server -f       # live log
```
Run the installer again to update. Desk IDs are kept in `/var/lib/remotedesk/desks.json`.
For connections from the internet, forward TCP port 7070 on your router to the Pi.
After installing, log out and in once: then `remotedesk-admin` and the server window work
without `sudo` (you are added to the `remotedesk` group).

**Server window** (menu entry "RemoteDesk Server"; installed automatically when the Pi boots
into a desktop, or with `--gui`): shows and edits desks, sessions and the log of the running
service, and starts / stops / restarts it (asks for your password). Closing the window does
not stop the service.

**Client** (app menu entry "RemoteDesk", command `remotedesk`):
```
cd RemoteDesk/linux
./install_client.sh                      # as your normal user; --uninstall to remove
```
Controlling other computers works everywhere. Being controlled:

| Desktop | Screen | Mouse + keyboard |
|---|---|---|
| X11 (Xorg; e.g. "Ubuntu on Xorg" on 24.04, XFCE, MATE, LXQt) | ✅ | ✅ |
| Wayland with GNOME (Ubuntu 24.04 / 26.04 default) or KDE Plasma | ✅ via the desktop portal | ✅ via the portal |
| Wayland with wlroots (Sway, labwc, Wayfire — e.g. Raspberry Pi OS) | ✅ via the portal | ❌ view only |
| No desktop (Ubuntu Server) | — nothing to share; the server works fine | |

On Wayland the desktop asks once for permission ("Allow remote interaction"); RemoteDesk keeps
the answer. For a Pi nobody sits at, open RemoteDesk once and click **Allow sharing…** under
*This Desk*. If the person at the computer declines, the viewer is told why.

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

## Android app
Control your computers from an Android phone or tablet (Android 7+), or let a computer
control the phone.

**Get the APK:** the latest build is committed to the branch as
`RemoteDesk/dist/RemoteDesk-android.apk` (open it on GitHub → *Download raw file*).
It is also attached to every build run on GitHub:
repository → **Actions** → *Android APK* → latest run → **Artifacts** → `RemoteDesk-android`
(a .zip containing `RemoteDesk-android.apk`). Copy it to the phone and open it; allow
"Install unknown apps" for your browser / file manager when asked.

Or build it yourself: open `RemoteDesk/android` in Android Studio → Run, or
`cd android && ./gradlew assembleDebug` (needs the Android SDK).

The APK is signed with a debug key that changes between builds, so to install a newer
build you may need to uninstall the old one first.

### Control a computer from the phone
Settings → Server = the same address as on the PCs, then enter a desk ID → Connect →
password. Direct addresses (`192.168.1.20`) and Tailscale names work without a server.

| Gesture | Touchpad mode (default) | Direct touch mode |
|---|---|---|
| One-finger drag | move the pointer | drag / select |
| Tap | left click at the pointer | click at that spot |
| Double tap + drag | drag / select | — |
| Long press / two-finger tap | right click | right click (long press) |
| Two-finger drag | scroll | scroll |
| Pinch | zoom (move to pan) | zoom (move to pan) |

Toolbar: **Keyboard**, **Keys** (Ctrl/Alt/Shift/Win stay pressed until the next key, Esc, Tab,
arrows, Home/End, PgUp/PgDn, F1–F12, Ctrl+Alt+Del), mouse mode, **Chat**, **Sound on/off**,
**More** (fit to screen, monitor, quality, picture: JPEG tiles / video stream, send clipboard,
fullscreen, disconnect). ▲ hides the toolbar. When someone else already controls the computer,
a "View only" badge shows and input is not sent.

### Share the phone (control Android from a PC)
Main screen → **Share this phone**:
1. **Setup** (once): enable *Remote control* (Settings → Accessibility → Installed apps →
   RemoteDesk), allow notifications, and allow running in the background.
2. **Start sharing**. The phone shows its address: the desk ID (with a server), its Tailscale
   address, or its Wi-Fi address — and a password.
3. On the PC, connect to that ID / address with the password. The phone asks **Allow?**, then
   Android asks to start recording — choose **Entire screen**. Further viewers only need **Allow**
   (they watch; the first one controls).

| On the PC | On the phone |
|---|---|
| click / drag | tap / swipe (hold > 0.5 s = long press) |
| right click | Back |
| middle click | Home |
| mouse wheel | scroll |
| typing, Backspace, Enter, arrows | edits the focused text field |
| Ctrl+C / V / X / A | copy / paste / cut / select all |
| Esc / Win / F1 / F2 | Back / Home / recent apps / notifications |

A red banner (with **Chat** and **End session**) and a notification show while a session runs.
The screen stays on during a session. Limits: Android needs the "start recording" confirmation
for each new session (so nobody can connect unnoticed), a locked phone shows only the lock
screen, some apps (banking, DRM video) appear black, and the phone sends JPEG tiles only and
no sound.

## Client features (PC)
- Desk ID + random password (show / copy / regenerate), recent sessions as tiles
  (click to connect, right-click to rename / remove), Tailscale devices tab
- **Several viewers per desk**: the first viewer allowed to control gets control, everyone else
  watches; control passes on when that viewer leaves. The accept prompt says who has control;
  the red banner lists everyone connected (with **Chat** and **End session**)
- **Chat** with sender and time, shared by everyone in the session (window on the desk, side
  panel in the viewer); a new message opens it
- Viewer: mouse / keyboard / wheel, Ctrl+Alt+Del, fullscreen, numbered **monitor buttons**,
  **Display** menu (picture: JPEG tiles or H.264 video stream; quality), **Sound** on/off,
  send clipboard, file transfer (button or drag & drop → `Downloads/RemoteDesk`), role
  indicator ("You have control" / "View only")
- **Sound**: what the desk plays (not its microphone), Opus ~64 kbit/s, in either picture mode
- Direct mode without a server: connect to `ip:7071` (LAN, VPN, Tailscale, IPv6)
- Settings: server address, direct port, unattended access, view-only

| Picture mode | Best for | Traffic |
|---|---|---|
| JPEG tiles | sharp text, office work | almost nothing while the screen is still |
| Video stream (H.264) | motion, scrolling, video | much less while things move |

## Server features
- **Permanent IDs**: the server assigns and stores desk IDs; a desk keeps its ID across server
  and client restarts. Desks are recognised by a machine key, or by computer name (e.g. after
  a reinstall; only while no online desk uses that name — can be turned off)
- Desks tab: every known desk with ID, computer name, status, IP, last seen; search,
  **Edit ID…** (an online desk is told its new ID at once), **Delete**
- Active sessions with **Disconnect**, live stats, event log
- Shows the addresses clients should use
- Windows / no service: start/stop, port, autostart, tray. Linux service: managed by the window
  or `remotedesk-admin`

## Security
End-to-end encrypted (X25519 key exchange confirmed with the password, AES-256-GCM).
The server only forwards ciphertext and never sees passwords, screens, chat or sound.
Wrong passwords are throttled. Use strong passwords, especially with unattended access.

## Limitations
- No printing, no peer-to-peer hole punching (ID sessions always go through the server)
- Wayland desktops based on wlroots (Sway, labwc, Wayfire) can only be watched, not controlled
- Android desk: JPEG tiles only, no sound; Android asks for recording permission each session
- Can't control the Windows login screen / UAC prompts (would need a Windows service)
- macOS: untested; would need Screen Recording + Accessibility permissions

## Files
| What | Windows | Linux |
|---|---|---|
| Client settings | `%USERPROFILE%\.remotedesk.json` | `~/.remotedesk.json` |
| Viewer display / sound choice | registry (QSettings) | `~/.config/RemoteDesk/Viewer.conf` |
| Wayland sharing permission | — | `~/.config/remotedesk/portal_token` |
| Server window settings | `%USERPROFILE%\.remotedesk_server.json` | `~/.remotedesk_server.json` |
| Desk IDs (server) | `%USERPROFILE%\.remotedesk_server_desks.json` | service: `/var/lib/remotedesk/desks.json` |

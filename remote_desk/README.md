# RemoteDesk

Self-hosted remote desktop in the spirit of AnyDesk: connect by a 9-digit ID
through your own relay server, or directly by `host:port` on a LAN/VPN.

## Features
- 9-digit desk ID via a self-hosted relay (works behind NAT, no port forwarding on the host)
- Direct LAN connections (`192.168.1.20` or `host:7071`)
- End-to-end encryption: X25519 + password-authenticated key confirmation, AES-256-GCM
  (the relay only forwards ciphertext and never learns the password)
- Incremental screen updates (64 px tiles, only changed tiles sent as JPEG)
- Full mouse/keyboard/wheel control, Ctrl+Alt+Del button, fullscreen
- Multi-monitor selection, quality presets (Best / Balanced / Fast)
- Clipboard push, text chat, file transfer (button or drag & drop → `~/Downloads/RemoteDesk`)
- Host safety: accept/deny prompt, visible "session active" banner with one-click end,
  view-only mode, wrong-password throttling, optional unattended access

## Install
```
pip install -r remote_desk/requirements.txt
```

## Run
Relay (on a server reachable by both sides, open TCP 7070):
```
python -m remote_desk.relay --port 7070
```
Each computer (from the repository root):
```
python -m remote_desk --relay relay.example.com:7070
```
The window shows **Your ID** and **Password**. On the other computer, type the ID
(or `ip:7071` for direct mode), click Connect and enter the password.

Settings are stored in `~/.remotedesk.json` (permissions 600).

## Limitations vs. AnyDesk
- No custom video codec: JPEG tiles work well on LAN and normal broadband, but
  full-screen video uses more bandwidth than H.264.
- No audio forwarding, no remote printing, no UDP hole punching (all
  traffic goes through the relay unless you connect directly).
- Wayland sessions on Linux block screen capture and input injection; use an X11 session.
- macOS requires Screen Recording and Accessibility permissions for Python.
- Running at the Windows login screen / UAC prompts needs a Windows service, which isn't included.
- The password is checked with PBKDF2, but a malicious relay could still try offline
  dictionary guesses. Use strong passwords (the default is random, 10 characters).

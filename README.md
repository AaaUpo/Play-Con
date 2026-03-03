# Play-Con

Play-Con synchronizes `mpv` playback state (file path, pause status, and position) across multiple clients joined in the same room.

## Requirements

- Python 3.9+
- `mpv` installed on each client machine
- `openssl` available on the server machine (used to generate self-signed TLS certs)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Server usage

The server now uses **one TLS port only**.

```bash
python server.py [flags]
```

### Server flags

- `--salt <value>`
  - Salt used for password hashing.
  - If omitted, a random salt is generated each run (room passwords won't survive restart).
- `--hide-ip`
  - Suppress printing the detected public IP in logs.
- `--port <int>`
  - TLS listening port.
  - Default: `6464`
- `--cert-host <host_or_ip>`
  - Hostname/IP written into generated TLS certificate CN/SAN.
  - If omitted, server tries to auto-detect public IP and use that.
- `--regenerate-cert`
  - Force re-creation of `server_cert.pem` and `server_key.pem`.

### Server examples

Use defaults:

```bash
python server.py
```

Use a fixed salt and explicit public IP in certificate identity:

```bash
python server.py --salt my-stable-salt --port 6464 --cert-host 151.55.120.50 --regenerate-cert
```

## Client usage

Client also uses **one TLS port only** (`--port`).

```bash
python client.py --room <room> --pass <password> --server-ip <ip_or_host> [flags]
```

### Client flags

- `--room <name>`
  - Room name to join/create.
  - Required.
- `--pass <password>`
  - Room password.
  - Required.
- `--server-ip <ip_or_host>`
  - Server address.
  - Required.
- `--port <int>`
  - Server TLS port.
  - Default: `3233`
- `--username <name>`
  - Name shown to others in room.
  - Default: value of `USER` env var or `anonymous`.
- `--mpv-path <path>`
  - Linux/macOS: IPC socket path for mpv (default `/tmp/mpv-socket`).
  - Windows:
    - If you pass path to `mpv.exe`, client launches it and uses default pipe `\\.\pipe\playcon-mpv`.
    - You can also pass a pipe path directly.
- `--hide-ip`
  - Hide server IP in local startup banner.

### Client examples

Windows client with explicit port:

```powershell
py client.py --room test --pass test --server-ip 151.55.120.50 --port 6464 --username oregon --mpv-path "C:\Users\X\Desktop\mpv\mpv.exe"
```

Linux/macOS client:

```bash
python client.py --room test --pass test --server-ip 151.55.120.50 --port 6464 --username alex --mpv-path /tmp/mpv-socket
```

## TLS certificate behavior

- On first connection, the client **always shows and asks** whether to trust the server certificate.
- If accepted, certificate is pinned to:
  - Linux/macOS: `~/.playcon_server_cert.pem`
  - Windows: `%USERPROFILE%\.playcon_server_cert.pem`
- On later connections, the presented certificate fingerprint must match the pinned one.
- If cert changes, client prints old/new fingerprints and asks again.

## Network checklist for internet play

1. Open/forward the server `--port` on router/firewall.
2. Ensure server process is running and listening.
3. Use the same room/password on all clients.
4. If testing from inside the same LAN, prefer local/LAN IP over public IP if your router lacks NAT loopback.


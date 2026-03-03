# Play-Con

A Python client/server sync tool for mpv.

## Features
- Room-based sync for `filename`, `position`, `paused`.
- TLS server with self-signed cert generation.
- Client certificate pinning prompt (TOFU) for remote TLS servers.
- Localhost plaintext probe (`127.0.0.1`) with TLS fallback.
- Client auto-starts mpv.
- Configurable `--port` on server and client.

## Requirements
- Python 3.10+
- `openssl` available on PATH (for server TLS cert generation)
- mpv installed

Optional:
- `python-mpv-jsonipc` (see `requirements.txt`)

## Server
```bash
python server.py --salt YOUR_SECRET_SALT --port 3232
```

Flags:
- `--salt`: salt used for room password hashing.
- `--hide-ip`: do not print public IP.
- `--port`: listen port (default `3232`).
- `--no-tls`: disable TLS (local testing only).

If `--salt` is omitted, server generates a random per-run salt and prints:
```text
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
WARNING: --salt was not provided. A random ephemeral salt was generated.
WARNING: generated salt for this run: --salt <generated>
WARNING: use --salt <value> to keep room passwords stable after restart.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

## Client
```bash
python client.py --room test --pass test --server-ip 127.0.0.1 --port 3232 --mpv-path mpv
```

Important flags:
- `--room`, `--pass`, `--server-ip`, `--port`, `--username`
- `--mpv-path`: path to mpv executable (client launches mpv itself)
- `--mpv-ipc`: optional custom IPC endpoint
- `--media`: optional media path/URL to open at startup
- `--hide-ip`

## Windows note
- Client launches mpv on Windows.
- mpv IPC control uses named pipes on Windows and is not yet implemented in this client.
  - Result: network sync still works, but local mpv event capture/apply is unavailable until pipe IPC is added.

## Security
- Remote connections use TLS.
- On first remote connection, client asks to trust/store server certificate in:
  - `~/.playcon-certs/<server>_<port>.pem`

## Troubleshooting
- `WinError 1225` / connection refused:
  - Verify server is running and listening on expected `--port`.
  - Check firewall/NAT port-forwarding.
  - Ensure client uses correct public/private IP.
- If TLS cert changes, client will ask to trust the new cert.

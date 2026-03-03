# Play-Con

Reworked sync server/client for stronger stability under real-world network drops and noisy playback updates.

## What changed

- Client now automatically reconnects with backoff if server connectivity drops.
- Client keeps mpv running during reconnect cycles instead of exiting on first disconnect.
- Client now sends low-frequency position updates (throttled) plus explicit seek/pause/load events.
- State apply logic avoids unloading by ignoring empty filename updates.
- Server validates incoming state data and keeps a monotonic room `revision`.
- TLS trust file location for the client is now **next to `client.py`** and must be named:
  - `server_cert.pem`

## TLS certificate placement (client)

Put the server certificate at:

- `./server_cert.pem` (same folder as `client.py`)

The client will load that file automatically in TLS mode.

## Quick start

### Server

```bash
python3 server.py --salt your-static-salt
```

### Client (auto mode: localhost plaintext, remote TLS)

```bash
python3 client.py --room demo --pass secret --server-ip <ip-or-host>
```

For remote TLS, ensure `server_cert.pem` is in the same directory as `client.py`.

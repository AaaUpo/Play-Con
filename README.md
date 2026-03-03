# Play-Con

Play-Con synchronizes `mpv` playback state (file, pause/play, time position) between users in a shared room.

- `server.py` exposes **two server ports**:
  - `--port`: plaintext (intended for local/host use)
  - `--tls-port`: TLS (intended for remote users)
- `client.py` uses **one port flag only** (`--port`):
  - when connecting to `localhost` / `127.0.0.1` / `::1`, it uses plaintext
  - otherwise it uses TLS and requires a trusted server certificate file

---

## Requirements

- Python 3.9+
- `mpv` installed on each client machine
- OpenSSL available on server host (used to generate self-signed certs)

Install Python deps:

```bash
pip install -r requirements.txt
```

---

## Server usage (`server.py`)

### Command

```bash
python server.py [FLAGS]
```

### Flags

- `--salt TEXT`
  - Salt used for room password hashing.
  - If omitted, a random one is generated each run (room passwords won’t remain stable between restarts).
- `--hide-ip`
  - Do not print discovered public IP on startup.
- `--port INT` (default: `3232`)
  - Plaintext listening port (host/local clients).
- `--tls-port INT` (default: `3233`)
  - TLS listening port (remote clients).

### Notes

- On first run, server auto-generates:
  - `server_cert.pem`
  - `server_key.pem`
- The certificate includes SAN entries for loopback and (when discoverable) the public IP.
- Remote clients must receive `server_cert.pem` from the host and pass it via `--server-cert`.

### Example

```bash
python server.py --salt my-fixed-salt --port 6464 --tls-port 6465
```

---

## Client usage (`client.py`)

### Command

```bash
python client.py --room ROOM --pass PASSWORD --server-ip HOST --port PORT [FLAGS]
```

### Required flags

- `--room TEXT`
  - Room name.
- `--pass TEXT`
  - Room password.
- `--server-ip TEXT`
  - Server hostname/IP.
- `--port INT`
  - Target port.

### Optional flags

- `--username TEXT` (default: `$USER` or `anonymous`)
  - Display name.
- `--mpv-path TEXT` (default: `/tmp/mpv-socket`)
  - Linux/macOS: mpv IPC socket path.
  - Windows: can be `mpv.exe` path; client uses named pipe IPC automatically.
- `--server-cert PATH` (default: `~/.playcon_server_cert.pem`)
  - Certificate file used to validate remote TLS server.
  - Required for non-localhost connections.
- `--hide-ip`
  - Hide printed server IP in the startup summary.

### TLS behavior

- Localhost target (`localhost`, `127.0.0.1`, `::1`) → plaintext connection.
- Non-local target → TLS connection.
  - If `--server-cert` file is missing, client exits with instructions.
  - If cert validation fails, client exits and asks for updated `server_cert.pem` from host.

### Examples

Host machine (plaintext local):

```bash
python client.py --room test --pass test --server-ip 127.0.0.1 --port 6464 --username host
```

Remote machine (TLS):

```bash
python client.py --room test --pass test --server-ip 151.55.120.50 --port 6465 --server-cert C:/path/to/server_cert.pem --username friend
```

Windows with explicit mpv binary path:

```bash
py client.py --room test --pass test --server-ip 151.55.120.50 --port 6465 --server-cert C:\certs\server_cert.pem --mpv-path "C:\mpv\mpv.exe" --username friend
```

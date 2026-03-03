#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import secrets
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set

HOST = "0.0.0.0"
DEFAULT_PORT = 3232
DEFAULT_TLS_PORT = 6464
CERT_FILE = Path("server_cert.pem")
KEY_FILE = Path("server_key.pem")


@dataclass
class RoomState:
    filename: str = ""
    position: float = 0.0
    paused: bool = True
    revision: int = 0

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "position": self.position,
            "paused": self.paused,
            "revision": self.revision,
        }


@dataclass(eq=False)
class ClientConn:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    username: str = "anonymous"
    room: Optional[str] = None
    client_id: str = field(default_factory=lambda: secrets.token_hex(6))

    def __hash__(self) -> int:
        return id(self)


@dataclass
class Room:
    password_value: Optional[str] = None
    clients: Set[ClientConn] = field(default_factory=set)
    state: RoomState = field(default_factory=RoomState)


class SyncServer:
    def __init__(self, salt: str):
        self.salt = salt
        self.rooms: Dict[str, Room] = {}

    def normalize_password(self, plain_password: str) -> str:
        return hashlib.sha256(f"{self.salt}:{plain_password}".encode("utf-8")).hexdigest()

    async def send(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def broadcast(self, room_name: str, payload: dict, exclude: Optional[ClientConn] = None) -> None:
        room = self.rooms.get(room_name)
        if not room:
            return
        stale: Set[ClientConn] = set()
        for client in room.clients:
            if exclude is client:
                continue
            try:
                await self.send(client.writer, payload)
            except Exception:
                stale.add(client)
        for client in stale:
            room.clients.discard(client)

    async def on_join(self, client: ClientConn, data: dict) -> None:
        room_name = data.get("room")
        password = data.get("password", "")
        username = data.get("username") or "anonymous"
        if not isinstance(room_name, str) or not room_name.strip():
            await self.send(client.writer, {"type": "error", "message": "Missing room name"})
            return

        room_name = room_name.strip()
        room = self.rooms.setdefault(room_name, Room())
        normalized = self.normalize_password(str(password))
        if room.password_value is None:
            room.password_value = normalized
        elif room.password_value != normalized:
            await self.send(client.writer, {"type": "error", "message": "Invalid room password"})
            return

        if client.room and client.room in self.rooms:
            self.rooms[client.room].clients.discard(client)

        client.room = room_name
        client.username = username
        room.clients.add(client)

        await self.send(
            client.writer,
            {
                "type": "joined",
                "room": room_name,
                "client_id": client.client_id,
                "state": {
                    **room.state.as_dict(),
                },
            },
        )
        print(f"[room:{room_name}] {username} joined ({len(room.clients)} connected)")
        await self.broadcast(room_name, {"type": "user_event", "event": "join", "username": username}, exclude=client)

    async def on_control_update(self, client: ClientConn, data: dict) -> None:
        if not client.room:
            await self.send(client.writer, {"type": "error", "message": "Join a room first"})
            return
        room = self.rooms.get(client.room)
        if not room:
            return

        event_name = str(data.get("event") or "state")

        incoming_filename = data.get("filename")
        if isinstance(incoming_filename, str):
            room.state.filename = incoming_filename

        try:
            incoming_position = float(data.get("position", room.state.position))
            if incoming_position >= 0:
                room.state.position = incoming_position
        except (TypeError, ValueError):
            pass

        if "paused" in data:
            room.state.paused = bool(data.get("paused"))
        room.state.revision += 1

        await self.broadcast(
            client.room,
            {
                "type": "sync_command",
                "by": client.username,
                "source_client_id": client.client_id,
                "event": event_name,
                "state": room.state.as_dict(),
            },
            exclude=client,
        )

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = ClientConn(reader=reader, writer=writer)
        peer = writer.get_extra_info("peername")
        print(f"[server] client connected: {peer}")
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=240)
                if not line:
                    break
                try:
                    data = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    await self.send(writer, {"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = data.get("type")
                if msg_type == "join":
                    await self.on_join(client, data)
                elif msg_type == "control_update":
                    await self.on_control_update(client, data)
                elif msg_type == "ping":
                    await self.send(writer, {"type": "pong"})
                else:
                    await self.send(writer, {"type": "error", "message": f"Unknown message type: {msg_type}"})
        except asyncio.TimeoutError:
            print(f"[server] client timed out: {peer}")
        except (ConnectionResetError, BrokenPipeError) as exc:
            print(f"[server] client connection dropped {peer}: {exc}")
        except Exception as exc:
            print(f"[server] client error {peer}: {exc}")
        finally:
            if client.room and client.room in self.rooms:
                room = self.rooms[client.room]
                room.clients.discard(client)
                if not room.clients:
                    self.rooms.pop(client.room, None)
                else:
                    await self.broadcast(client.room, {"type": "user_event", "event": "leave", "username": client.username})
                print(f"[room:{client.room}] {client.username} left ({len(room.clients)} connected)")

            try:
                writer.close()
            except Exception:
                pass
            try:
                await writer.wait_closed()
            except (ConnectionResetError, OSError):
                pass
            print(f"[server] client disconnected: {peer}")


def _build_self_signed_openssl_config(public_ip: Optional[str]) -> str:
    san_entries = ["DNS:localhost", "IP:127.0.0.1", "IP:0.0.0.0"]
    if public_ip:
        san_entries.append(f"IP:{public_ip}")

    return f"""
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = Play-Con-Server

[v3_req]
subjectAltName = {', '.join(san_entries)}
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
""".strip()


def ensure_self_signed_cert(cert_file: Path, key_file: Path, public_ip: Optional[str]) -> None:
    if cert_file.exists() and key_file.exists():
        print(f"[server] Reusing existing TLS cert/key: {cert_file}, {key_file}")
        return

    print("[server] Generating self-signed TLS certificate via openssl...")
    cfg = _build_self_signed_openssl_config(public_ip)
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as tf:
        tf.write(cfg)
        config_path = tf.name

    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key_file),
        "-out",
        str(cert_file),
        "-days",
        "365",
        "-config",
        config_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        print("[server] ERROR: openssl not found on PATH.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[server] ERROR: openssl certificate generation timed out.")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"[server] ERROR: openssl failed: {exc.stderr or exc}")
        sys.exit(1)
    finally:
        Path(config_path).unlink(missing_ok=True)

    print(f"[server] Created {cert_file}. Share this file securely with TLS clients.")


def get_public_ip(timeout: float = 3.0) -> Optional[str]:
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=timeout) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as exc:
        print(f"[server] Could not fetch public IP: {exc}")
        return None


async def run_server(args: argparse.Namespace) -> None:
    active_salt = args.salt
    if not active_salt:
        active_salt = secrets.token_urlsafe(16)
        print("\n" + "!" * 72)
        print("WARNING: --salt was not provided. A random ephemeral salt was generated.")
        print(f"WARNING: generated salt for this run: {active_salt}")
        print(f"WARNING: use --salt {active_salt} to keep room passwords stable after restart.")
        print("!" * 72 + "\n")

    if args.port == args.tls_port:
        raise ValueError("--port and --tls-port must be different")

    public_ip: Optional[str] = None
    if not args.hide_ip:
        public_ip = get_public_ip()
        if public_ip:
            print(f"[server] Public IP: {public_ip}")

    ensure_self_signed_cert(CERT_FILE, KEY_FILE, public_ip)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))

    sync_server = SyncServer(salt=active_salt)
    plain_server = await asyncio.start_server(sync_server.handle_client, HOST, args.port)
    tls_server = await asyncio.start_server(sync_server.handle_client, HOST, args.tls_port, ssl=ssl_ctx)

    plain_addrs = ", ".join(str(sock.getsockname()) for sock in plain_server.sockets or [])
    tls_addrs = ", ".join(str(sock.getsockname()) for sock in tls_server.sockets or [])
    print(f"[server] Plaintext listening on {plain_addrs}")
    print(f"[server] TLS listening on {tls_addrs} (0.0.0.0 means all local interfaces)")
    if public_ip:
        print(f"[server] External TLS endpoint: {public_ip}:{args.tls_port}")

    async with plain_server, tls_server:
        await asyncio.gather(
            plain_server.serve_forever(),
            tls_server.serve_forever(),
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Play-Con mpv sync server")
    p.add_argument("--salt", help="Server-side salt used to hash room passwords")
    p.add_argument("--hide-ip", action="store_true", help="Hide public IP output on startup")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Plaintext local port (default: {DEFAULT_PORT})")
    p.add_argument("--tls-port", type=int, default=DEFAULT_TLS_PORT, help=f"TLS external port (default: {DEFAULT_TLS_PORT})")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run_server(parse_args()))
    except KeyboardInterrupt:
        print("\n[server] Shutdown requested.")

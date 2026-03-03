#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set

HOST = "0.0.0.0"
PORT = 3232
CERT_FILE = Path("server_cert.pem")
KEY_FILE = Path("server_key.pem")


@dataclass
class RoomState:
    filename: str = ""
    position: float = 0.0
    paused: bool = True


@dataclass(eq=False)
class ClientConn:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    username: str = "anonymous"
    room: Optional[str] = None

    def __hash__(self) -> int:
        return id(self)


@dataclass
class Room:
    password_value: Optional[str] = None
    clients: Set[ClientConn] = field(default_factory=set)
    state: RoomState = field(default_factory=RoomState)


class SyncServer:
    def __init__(self, salt: Optional[str] = None):
        self.salt = salt
        self.rooms: Dict[str, Room] = {}

    def normalize_password(self, plain_password: str) -> str:
        if self.salt is None:
            return plain_password
        payload = f"{self.salt}:{plain_password}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def send(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        msg = json.dumps(payload, ensure_ascii=False) + "\n"
        writer.write(msg.encode("utf-8"))
        await writer.drain()

    async def broadcast(self, room_name: str, payload: dict, exclude: Optional[ClientConn] = None) -> None:
        room = self.rooms.get(room_name)
        if not room:
            return
        stale: Set[ClientConn] = set()
        for client in room.clients:
            if exclude is not None and client is exclude:
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

        if not room_name:
            await self.send(client.writer, {"type": "error", "message": "Missing room name"})
            return

        normalized = self.normalize_password(password)
        room = self.rooms.setdefault(room_name, Room())

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
                "state": {
                    "filename": room.state.filename,
                    "position": room.state.position,
                    "paused": room.state.paused,
                },
            },
        )

        await self.broadcast(
            room_name,
            {"type": "user_event", "event": "join", "username": username},
            exclude=client,
        )

    async def on_state_update(self, client: ClientConn, data: dict) -> None:
        if not client.room:
            await self.send(client.writer, {"type": "error", "message": "Join a room first"})
            return

        room = self.rooms.get(client.room)
        if not room:
            return

        room.state.filename = data.get("filename", room.state.filename)
        room.state.position = float(data.get("position", room.state.position))
        room.state.paused = bool(data.get("paused", room.state.paused))

        payload = {
            "type": "state_update",
            "by": client.username,
            "state": {
                "filename": room.state.filename,
                "position": room.state.position,
                "paused": room.state.paused,
            },
        }
        await self.broadcast(client.room, payload, exclude=client)

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
                elif msg_type == "state_update":
                    await self.on_state_update(client, data)
                elif msg_type == "ping":
                    await self.send(writer, {"type": "pong"})
                else:
                    await self.send(writer, {"type": "error", "message": f"Unknown message type: {msg_type}"})
        except asyncio.TimeoutError:
            print(f"[server] client timed out: {peer}")
        except Exception as exc:
            print(f"[server] client error {peer}: {exc}")
        finally:
            if client.room and client.room in self.rooms:
                room = self.rooms[client.room]
                room.clients.discard(client)
                if not room.clients:
                    self.rooms.pop(client.room, None)
                else:
                    await self.broadcast(
                        client.room,
                        {"type": "user_event", "event": "leave", "username": client.username},
                    )
            writer.close()
            await writer.wait_closed()
            print(f"[server] client disconnected: {peer}")


def ensure_self_signed_cert(cert_file: Path, key_file: Path) -> None:
    if cert_file.exists() and key_file.exists():
        return

    print("[server] Generating self-signed TLS certificate via openssl...")
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
        "-subj",
        "/CN=Play-Con-Server",
    ]
    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=20)
        if res.stdout.strip():
            print(res.stdout)
        if res.stderr.strip():
            print(res.stderr)
    except FileNotFoundError:
        print("[server] ERROR: openssl was not found on PATH.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[server] ERROR: openssl certificate generation timed out.")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"[server] ERROR: openssl failed: {exc.stderr or exc}")
        sys.exit(1)


def get_public_ip(timeout: float = 3.0) -> Optional[str]:
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=timeout) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as exc:
        print(f"[server] Could not fetch public IP: {exc}")
        return None


async def run_server(args: argparse.Namespace) -> None:
    ensure_self_signed_cert(CERT_FILE, KEY_FILE)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))

    sync_server = SyncServer(salt=args.salt)

    if args.salt is None:
        print("\n" + "!" * 72)
        print("WARNING: server started without --salt. Room passwords are stored in plain form.")
        print("!" * 72 + "\n")

    if not args.hide_ip:
        public_ip = get_public_ip()
        if public_ip:
            print(f"[server] Public IP: {public_ip}")

    server = await asyncio.start_server(sync_server.handle_client, HOST, PORT, ssl=ssl_ctx)
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"[server] Listening on {addrs}")

    async with server:
        await server.serve_forever()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Play-Con mpv sync server")
    p.add_argument("--salt", help="Server-side salt used to hash room passwords")
    p.add_argument("--hide-ip", action="store_true", help="Hide public IP output on startup")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run_server(parse_args()))
    except KeyboardInterrupt:
        print("\n[server] Shutdown requested.")

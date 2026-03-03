#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_PORT = 3232
DEFAULT_MPV_SOCKET = "/tmp/mpv-socket"
CERT_STORE = Path.home() / ".playcon_server_cert.pem"


class MPVIPC:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.request_id = 1

    async def connect(self) -> None:
        if not hasattr(asyncio, "open_unix_connection"):
            raise RuntimeError("This Python runtime does not support Unix socket IPC (common on Windows)")
        if self.socket_path.lower().endswith(".exe"):
            raise RuntimeError("--mpv-path must be an mpv IPC socket path, not mpv.exe")
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)

    async def command(self, command: list) -> None:
        if not self.writer:
            return
        self.writer.write((json.dumps({"command": command, "request_id": self.request_id}) + "\n").encode("utf-8"))
        self.request_id += 1
        await self.writer.drain()

    async def observe(self) -> None:
        await self.command(["observe_property", 1, "pause"])
        await self.command(["observe_property", 2, "time-pos"])
        await self.command(["observe_property", 3, "path"])


class SyncClient:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.room = args.room
        self.password = args.password
        self.username = args.username
        self.server_ip = args.server_ip
        self.port = args.port
        self.mpv = MPVIPC(args.mpv_path)
        self.server_reader: Optional[asyncio.StreamReader] = None
        self.server_writer: Optional[asyncio.StreamWriter] = None
        self.current_state: Dict[str, Any] = {"filename": "", "position": 0.0, "paused": True}
        self.apply_remote = False

    async def send_server(self, payload: dict) -> None:
        if self.server_writer:
            self.server_writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self.server_writer.drain()

    async def read_server(self, timeout: float = 8.0) -> Dict[str, Any]:
        if not self.server_reader:
            raise ConnectionError("Server reader unavailable")
        line = await asyncio.wait_for(self.server_reader.readline(), timeout=timeout)
        if not line:
            raise ConnectionError("Server closed connection")
        return json.loads(line.decode("utf-8"))

    async def print_header(self) -> None:
        ip_display = "(hidden)" if self.args.hide_ip else self.server_ip
        print("=" * 72)
        print(f"Room     : {self.room}")
        print(f"Password : {self.password}")
        print(f"Username : {self.username}")
        print(f"Server IP: {ip_display}")
        print(f"Port     : {self.port}")
        print("=" * 72)

    async def _open_plain(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(asyncio.open_connection(self.server_ip, self.port), timeout=7)

    async def _open_tls(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(self._connect_tls_with_validation(), timeout=10)

    async def establish_server_connection(self) -> None:
        if self.server_ip == "127.0.0.1":
            print("[client] Trying localhost plain TCP first...")
            try:
                self.server_reader, self.server_writer = await self._open_plain()
                await self.send_server({"type": "ping"})
                msg = await self.read_server(timeout=2)
                if msg.get("type") != "pong":
                    raise ConnectionError(f"Unexpected probe reply: {msg}")
                print("[client] Using plain TCP mode for localhost.")
                return
            except Exception as exc:
                print(f"[client] Plain localhost mode failed ({exc}); falling back to TLS.")
                await self._close_server_writer()

        self.server_reader, self.server_writer = await self._open_tls()
        print("[client] Using TLS mode.")

    async def _connect_tls_with_validation(self):
        def make_ctx() -> ssl.SSLContext:
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ctx.check_hostname = False
            if CERT_STORE.exists():
                ctx.load_verify_locations(cafile=str(CERT_STORE))
            else:
                ctx.verify_mode = ssl.CERT_NONE
            return ctx

        ctx = make_ctx()
        try:
            return await asyncio.open_connection(self.server_ip, self.port, ssl=ctx, server_hostname="Play-Con-Server")
        except ssl.SSLCertVerificationError as exc:
            print(f"[client] Stored cert invalid or changed: {exc}")
            if not await self._prompt_trust_and_store_cert():
                raise RuntimeError("User rejected new server certificate") from exc
            return await asyncio.open_connection(self.server_ip, self.port, ssl=make_ctx(), server_hostname="Play-Con-Server")

    async def _prompt_trust_and_store_cert(self) -> bool:
        pem = await asyncio.to_thread(ssl.get_server_certificate, (self.server_ip, self.port))
        print("[client] Retrieved server certificate:")
        print("-" * 72)
        print("\n".join(pem.splitlines()[:8]))
        print("...\n" + "-" * 72)
        answer = await asyncio.to_thread(input, "Trust and store this certificate? [y/N]: ")
        if answer.strip().lower() != "y":
            return False
        CERT_STORE.write_text(pem, encoding="utf-8")
        print(f"[client] Certificate saved to {CERT_STORE}")
        return True

    async def _close_server_writer(self) -> None:
        if self.server_writer:
            self.server_writer.close()
            try:
                await self.server_writer.wait_closed()
            except Exception:
                pass
        self.server_reader = None
        self.server_writer = None

    async def run(self) -> None:
        await self.print_header()
        await self.establish_server_connection()
        print("[client] Connected to server.")

        await self.send_server({
            "type": "join",
            "room": self.room,
            "password": self.password,
            "username": self.username,
        })

        try:
            first = await self.read_server(timeout=8)
        except Exception as exc:
            raise RuntimeError(f"Join handshake failed: {exc}") from exc

        if first.get("type") == "error":
            raise RuntimeError(f"Server rejected join: {first.get('message')}")
        if first.get("type") != "joined":
            raise RuntimeError(f"Unexpected first server reply: {first}")

        print(f"[server] Joined room {first.get('room')}")
        state = first.get("state", {})
        await self.apply_state_to_mpv(state)
        print(f"[update] initial state: {state}")

        try:
            await self.mpv.connect()
            await self.mpv.observe()
            print(f"[client] Connected to mpv IPC socket at {self.args.mpv_path}")
        except Exception as exc:
            print(f"[client] Warning: mpv IPC unavailable ({exc})")

        tasks = [asyncio.create_task(self.server_listener()), asyncio.create_task(self.mpv_listener())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                raise exc

    async def server_listener(self) -> None:
        while True:
            data = await self.read_server(timeout=240)
            msg_type = data.get("type")
            if msg_type == "error":
                print(f"[server:error] {data.get('message')}")
            elif msg_type == "state_update":
                state = data.get("state", {})
                print(f"[update] by {data.get('by')}: {state}")
                await self.apply_state_to_mpv(state)
            elif msg_type == "user_event":
                print(f"[room] {data.get('username')} {data.get('event')}ed")
            elif msg_type == "pong":
                print("[server] pong")
            else:
                print(f"[server] {data}")

    async def apply_state_to_mpv(self, state: Dict[str, Any]) -> None:
        if not self.mpv.writer:
            self.current_state.update(state)
            return
        self.apply_remote = True
        try:
            filename = state.get("filename")
            if filename and filename != self.current_state.get("filename"):
                await self.mpv.command(["loadfile", filename, "replace"])
            if "position" in state:
                await self.mpv.command(["set_property", "time-pos", float(state["position"])])
            if "paused" in state:
                await self.mpv.command(["set_property", "pause", bool(state["paused"])])
            self.current_state.update(state)
        finally:
            await asyncio.sleep(0.1)
            self.apply_remote = False

    async def mpv_listener(self) -> None:
        if not self.mpv.reader:
            while True:
                await asyncio.sleep(60)

        while True:
            line = await asyncio.wait_for(self.mpv.reader.readline(), timeout=240)
            if not line:
                raise ConnectionError("mpv IPC closed")
            try:
                data = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if self.apply_remote or data.get("event") != "property-change":
                continue

            name = data.get("name")
            value = data.get("data")
            changed = False
            if name == "pause" and isinstance(value, bool):
                self.current_state["paused"] = value
                changed = True
            elif name == "time-pos" and isinstance(value, (float, int)):
                self.current_state["position"] = float(value)
                changed = True
            elif name == "path" and isinstance(value, str):
                self.current_state["filename"] = value
                changed = True

            if changed:
                print(f"[mpv] local update: {self.current_state}")
                await self.send_server({"type": "state_update", **self.current_state})


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Play-Con mpv sync client")
    p.add_argument("--room", required=True, help="Room name")
    p.add_argument("--pass", dest="password", required=True, help="Room password")
    p.add_argument("--mpv-path", default=DEFAULT_MPV_SOCKET, help="Path to mpv JSON-IPC socket")
    p.add_argument("--hide-ip", action="store_true", help="Hide printed server IP in header")
    p.add_argument("--server-ip", required=True, help="Server IP or hostname")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")
    p.add_argument("--username", default=os.getenv("USER", "anonymous"), help="Display username")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(SyncClient(parse_args()).run())
    except KeyboardInterrupt:
        print("\n[client] Shutdown requested.")
    except Exception as exc:
        print(f"[client] Fatal error: {exc}")
        sys.exit(1)

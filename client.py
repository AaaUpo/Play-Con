#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import ssl
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_PORT = 3232
DEFAULT_MPV_SOCKET = "/tmp/mpv-socket"
CERT_STORE_DIR = Path.home() / ".playcon-certs"


class MPVIPC:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.request_id = 1

    async def connect(self) -> None:
        if os.name == "nt":
            raise RuntimeError(
                "Windows build detected: Unix sockets are unavailable here. "
                "Start mpv with --input-ipc-server=\\\\.\\pipe\\mpv and add pipe support in client, "
                "or run this client in WSL/Linux."
            )
        if not hasattr(asyncio, "open_unix_connection"):
            raise RuntimeError("This Python runtime does not support Unix socket IPC")
        if self.socket_path.lower().endswith(".exe"):
            raise RuntimeError("--mpv-path must be an IPC socket path, not mpv.exe")
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
        self.mpv = MPVIPC(args.mpv_ipc)
        self.server_reader: Optional[asyncio.StreamReader] = None
        self.server_writer: Optional[asyncio.StreamWriter] = None
        self.mpv_process: Optional[asyncio.subprocess.Process] = None
        self.current_state: Dict[str, Any] = {"filename": "", "position": 0.0, "paused": True}
        self.apply_remote = False
        self.mpv_ready = False

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
        return await asyncio.wait_for(asyncio.open_connection(self.server_ip, self.port), timeout=8)

    async def _open_tls(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(self._connect_tls_with_validation(), timeout=10)

    def _is_local_server(self) -> bool:
        return self.server_ip in {"127.0.0.1", "localhost", "::1"}

    def _cert_store_for_server(self) -> Path:
        safe_host = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in self.server_ip)
        return CERT_STORE_DIR / f"{safe_host}_{self.port}.pem"

    async def establish_server_connection(self) -> None:
        if self._is_local_server():
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

        try:
            self.server_reader, self.server_writer = await self._open_tls()
        except OSError as exc:
            raise RuntimeError(
                f"Could not connect to {self.server_ip}:{self.port} over TLS ({exc}). "
                "Check server is running, port forwarding/firewall, and matching --port."
            ) from exc
        print("[client] Using TLS mode.")

    async def _connect_tls_with_validation(self):
        cert_store = self._cert_store_for_server()

        def make_ctx() -> ssl.SSLContext:
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            ctx.check_hostname = False
            if cert_store.exists():
                ctx.load_verify_locations(cafile=str(cert_store))
            else:
                ctx.verify_mode = ssl.CERT_NONE
            return ctx

        if not self._is_local_server() and not cert_store.exists():
            print(f"[client] No stored certificate for {self.server_ip}:{self.port}.")
            if not await self._prompt_trust_and_store_cert(cert_store):
                raise RuntimeError("User rejected server certificate")

        try:
            return await asyncio.open_connection(self.server_ip, self.port, ssl=make_ctx(), server_hostname="Play-Con-Server")
        except ssl.SSLCertVerificationError as exc:
            print(f"[client] Stored cert invalid or changed: {exc}")
            if not await self._prompt_trust_and_store_cert(cert_store):
                raise RuntimeError("User rejected new server certificate") from exc
            return await asyncio.open_connection(self.server_ip, self.port, ssl=make_ctx(), server_hostname="Play-Con-Server")

    async def _prompt_trust_and_store_cert(self, cert_store: Path) -> bool:
        pem = await asyncio.to_thread(ssl.get_server_certificate, (self.server_ip, self.port))
        print("[client] Retrieved server certificate:")
        print("-" * 72)
        print("\n".join(pem.splitlines()[:8]))
        print("...\n" + "-" * 72)
        answer = await asyncio.to_thread(input, "Trust and store this certificate? [y/N]: ")
        if answer.strip().lower() != "y":
            return False
        cert_store.parent.mkdir(parents=True, exist_ok=True)
        cert_store.write_text(pem, encoding="utf-8")
        print(f"[client] Certificate saved to {cert_store}")
        return True

    async def start_mpv(self) -> None:
        if os.name == "nt":
            raise RuntimeError("Windows mpv IPC is not supported by this client build")

        with suppress(FileNotFoundError):
            os.unlink(self.args.mpv_ipc)

        cmd = [
            self.args.mpv_path,
            f"--input-ipc-server={self.args.mpv_ipc}",
            "--idle=yes",
            "--force-window=yes",
        ]
        if self.args.media:
            cmd.append(self.args.media)

        self.mpv_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        for _ in range(50):
            if Path(self.args.mpv_ipc).exists():
                break
            if self.mpv_process.returncode is not None:
                raise RuntimeError("mpv exited before opening IPC socket")
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for mpv IPC socket")

        await self.mpv.connect()
        await self.mpv.observe()
        self.mpv_ready = True
        print(f"[client] Started mpv and connected IPC at {self.args.mpv_ipc}")

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

        first = await self.read_server(timeout=10)
        if first.get("type") == "error":
            raise RuntimeError(f"Server rejected join: {first.get('message')}")
        if first.get("type") != "joined":
            raise RuntimeError(f"Unexpected first server reply: {first}")

        print(f"[server] Joined room {first.get('room')}")
        try:
            await self.start_mpv()
        except Exception as exc:
            raise RuntimeError(f"Unable to auto-start mpv with IPC: {exc}") from exc

        state = first.get("state", {})
        await self.apply_state_to_mpv(state)
        print(f"[update] initial state: {state}")

        tasks = [asyncio.create_task(self.server_listener(), name="server_listener")]
        if self.mpv_ready:
            tasks.append(asyncio.create_task(self.mpv_listener(), name="mpv_listener"))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                raise exc

    async def shutdown(self) -> None:
        await self._close_server_writer()
        if self.mpv.writer:
            self.mpv.writer.close()
            with suppress(Exception):
                await self.mpv.writer.wait_closed()
        if self.mpv_process and self.mpv_process.returncode is None:
            self.mpv_process.terminate()
            with suppress(ProcessLookupError):
                await asyncio.wait_for(self.mpv_process.wait(), timeout=3)

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
    p.add_argument("--mpv-path", default="mpv", help="Path to mpv executable")
    p.add_argument("--mpv-ipc", default=DEFAULT_MPV_SOCKET, help="Path to mpv JSON-IPC socket")
    p.add_argument("--media", help="Optional media file/URL to open in mpv at startup")
    p.add_argument("--hide-ip", action="store_true", help="Hide printed server IP in header")
    p.add_argument("--server-ip", required=True, help="Server IP or hostname")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")
    p.add_argument("--username", default=os.getenv("USER", "anonymous"), help="Display username")
    return p.parse_args()


if __name__ == "__main__":
    client = SyncClient(parse_args())
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\n[client] Shutdown requested.")
    except Exception as exc:
        print(f"[client] Fatal error: {exc}")
        sys.exit(1)
    finally:
        with suppress(Exception):
            asyncio.run(client.shutdown())

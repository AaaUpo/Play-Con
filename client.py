#!/usr/bin/env python3
import argparse
import asyncio
import errno
import json
import os
import ssl
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_PORT = 3232
DEFAULT_MPV_SOCKET = "/tmp/mpv-socket"
DEFAULT_MPV_PIPE = r"\\.\pipe\playcon-mpv"
DEFAULT_CERT_PATH = Path.home() / ".playcon_server_cert.pem"


class MPVIPC:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.request_id = 1

        self._pipe_file = None
        self._pipe_queue: Optional[asyncio.Queue[bytes]] = None
        self._pipe_read_thread: Optional[threading.Thread] = None
        self._pipe_write_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def is_connected(self) -> bool:
        return (self.writer is not None) or (self._pipe_file is not None)

    async def connect(self) -> None:
        if os.name == "nt":
            await self._connect_windows_pipe()
            return
        if not hasattr(asyncio, "open_unix_connection"):
            raise RuntimeError("This Python runtime does not support Unix socket IPC")
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)

    async def _connect_windows_pipe(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._pipe_queue = asyncio.Queue()
        self._pipe_file = await asyncio.to_thread(open, self.socket_path, "r+b", 0)

        def _reader() -> None:
            try:
                while True:
                    line = self._pipe_file.readline()
                    if not line:
                        break
                    if self._loop and self._pipe_queue:
                        self._loop.call_soon_threadsafe(self._pipe_queue.put_nowait, line)
            except Exception:
                pass
            finally:
                if self._loop and self._pipe_queue:
                    self._loop.call_soon_threadsafe(self._pipe_queue.put_nowait, b"")

        self._pipe_read_thread = threading.Thread(target=_reader, daemon=True)
        self._pipe_read_thread.start()

    async def readline(self) -> bytes:
        if os.name == "nt":
            if not self._pipe_queue:
                return b""
            return await self._pipe_queue.get()
        if not self.reader:
            return b""
        return await self.reader.readline()

    async def command(self, command: list) -> None:
        payload = (json.dumps({"command": command, "request_id": self.request_id}) + "\n").encode("utf-8")
        self.request_id += 1

        if os.name == "nt":
            if not self._pipe_file:
                return

            def _write() -> None:
                with self._pipe_write_lock:
                    self._pipe_file.write(payload)
                    self._pipe_file.flush()

            await asyncio.to_thread(_write)
            return

        if not self.writer:
            return
        self.writer.write(payload)
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
        self.use_tls = args.tls
        self.cert_path = Path(args.server_cert).expanduser().resolve() if args.server_cert else DEFAULT_CERT_PATH

        self.mpv_exec, self.mpv_ipc_path = self._resolve_mpv_launch_settings(args.mpv_path)
        self.mpv = MPVIPC(self.mpv_ipc_path)

        self.server_reader: Optional[asyncio.StreamReader] = None
        self.server_writer: Optional[asyncio.StreamWriter] = None
        self.current_state: Dict[str, Any] = {"filename": "", "position": 0.0, "paused": True}
        self.apply_remote = False
        self.mpv_process: Optional[asyncio.subprocess.Process] = None

        self._last_tick = time.monotonic()
        self._last_position = 0.0

    def _resolve_mpv_launch_settings(self, mpv_path_arg: str) -> Tuple[str, str]:
        if os.name != "nt":
            if mpv_path_arg.lower().endswith(".exe"):
                raise RuntimeError("--mpv-path must be an IPC socket path on Linux/macOS, not an .exe file")
            return "mpv", mpv_path_arg

        raw = mpv_path_arg.strip().strip('"')
        if raw.lower().endswith(".exe"):
            print("[client] Windows detected: --mpv-path points to mpv.exe; treating it as mpv executable path.")
            return raw, DEFAULT_MPV_PIPE

        if raw.startswith(r"\\.\pipe\\"):
            return "mpv.exe", raw

        return "mpv.exe", DEFAULT_MPV_PIPE

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
        mode = "TLS" if self.use_tls else "PLAINTEXT"
        print("=" * 72)
        print(f"Room      : {self.room}")
        print(f"Password  : {self.password}")
        print(f"Username  : {self.username}")
        print(f"Server IP : {ip_display}")
        print(f"Port      : {self.port}")
        print(f"Mode      : {mode}")
        print("=" * 72)

    async def establish_server_connection(self) -> None:
        if self.use_tls:
            self.server_reader, self.server_writer = await self._open_tls()
            print(f"[client] Using TLS mode with certificate: {self.cert_path}")
            return

        try:
            self.server_reader, self.server_writer = await asyncio.wait_for(
                asyncio.open_connection(self.server_ip, self.port),
                timeout=8,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not connect to server at {self.server_ip}:{self.port} over plaintext ({exc}). "
                "Check server --port and firewall settings."
            ) from exc
        print("[client] Using plaintext mode.")

    async def _open_tls(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if not self.cert_path.exists():
            raise RuntimeError(
                "TLS mode requires a trusted server certificate file, but none was found. "
                f"Expected: {self.cert_path}. Copy 'server_cert.pem' from the host running server.py "
                "to this client machine, then pass it with --server-cert (or place it at the default path)."
            )

        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=str(self.cert_path))

        try:
            return await asyncio.wait_for(
                asyncio.open_connection(self.server_ip, self.port, ssl=ctx, server_hostname="Play-Con-Server"),
                timeout=10,
            )
        except ssl.SSLError as exc:
            raise RuntimeError(
                f"TLS verification failed with certificate {self.cert_path}: {exc}. "
                "Make sure this certificate exactly matches the server's current server_cert.pem."
            ) from exc
        except OSError as exc:
            details = [
                f"Could not connect to remote server at {self.server_ip}:{self.port} over TLS ({exc}).",
                "Check server is running, firewall/port-forwarding, and that you're using server --tls-port.",
            ]
            if isinstance(exc, ConnectionRefusedError) or getattr(exc, "errno", None) in {
                errno.ECONNREFUSED,
                getattr(errno, "WSAECONNREFUSED", 10061),
            }:
                details.append(
                    "If you are on the same LAN as the host, use the host's LAN IP instead of public IP to avoid NAT loopback issues."
                )
            raise RuntimeError(" ".join(details)) from exc

    async def start_mpv(self) -> None:
        if os.name != "nt":
            socket_path = Path(self.mpv_ipc_path)
            if socket_path.exists():
                mode = socket_path.stat().st_mode
                if stat.S_ISSOCK(mode):
                    socket_path.unlink(missing_ok=True)

        cmd = [
            self.mpv_exec,
            "--idle=yes",
            "--force-window=yes",
            f"--input-ipc-server={self.mpv_ipc_path}",
        ]
        print(f"[client] Launching mpv: {' '.join(cmd)}")
        self.mpv_process = await asyncio.create_subprocess_exec(*cmd)

        for _ in range(70):
            if self.mpv_process.returncode is not None:
                raise RuntimeError("mpv exited immediately after launch")
            try:
                await self.mpv.connect()
                await self.mpv.observe()
                print(f"[client] Connected to mpv IPC at {self.mpv_ipc_path}")
                return
            except Exception:
                await asyncio.sleep(0.1)

        raise RuntimeError(f"Timed out waiting for mpv IPC at {self.mpv_ipc_path}")

    async def wait_for_mpv_exit(self) -> None:
        if not self.mpv_process:
            return
        code = await self.mpv_process.wait()
        raise RuntimeError(f"mpv exited (code {code}), stopping client")

    async def shutdown_mpv(self) -> None:
        if not self.mpv_process or self.mpv_process.returncode is not None:
            return
        self.mpv_process.terminate()
        try:
            await asyncio.wait_for(self.mpv_process.wait(), timeout=2)
        except asyncio.TimeoutError:
            self.mpv_process.kill()
            await self.mpv_process.wait()

    async def run(self) -> None:
        await self.print_header()
        await self.start_mpv()
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
        state = first.get("state", {})
        await self.apply_state_to_mpv(state)
        print(f"[update] initial state: {state}")

        tasks = [
            asyncio.create_task(self.server_listener(), name="server_listener"),
            asyncio.create_task(self.mpv_listener(), name="mpv_listener"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat_loop"),
            asyncio.create_task(self.wait_for_mpv_exit(), name="mpv_watchdog"),
        ]

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    raise exc
        finally:
            await self.shutdown_mpv()

    async def server_listener(self) -> None:
        while True:
            data = await self.read_server(timeout=240)
            msg_type = data.get("type")
            if msg_type == "error":
                print(f"[server:error] {data.get('message')}")
            elif msg_type in {"state_update", "sync_seek"}:
                state = data.get("state", {})
                print(f"[sync] {msg_type} from {data.get('by', 'server')}: {state}")
                await self.apply_state_to_mpv(state)
            elif msg_type == "user_event":
                event = data.get("event")
                username = data.get("username", "unknown")
                if event == "join":
                    print(f"[room] {username} entered the room")
                elif event == "leave":
                    print(f"[room] {username} left the room")
                else:
                    print(f"[room] {username} {event}")
            elif msg_type == "pong":
                print("[server] pong")
            else:
                print(f"[server] {data}")

    async def apply_state_to_mpv(self, state: Dict[str, Any]) -> None:
        if not self.mpv.is_connected():
            self.current_state.update(state)
            return

        self.apply_remote = True
        try:
            filename = state.get("filename")
            if filename and filename != self.current_state.get("filename"):
                await self.mpv.command(["loadfile", filename, "replace"])
            if "position" in state and state["position"] is not None:
                await self.mpv.command(["set_property", "time-pos", float(state["position"])])
            if "paused" in state:
                await self.mpv.command(["set_property", "pause", bool(state["paused"])])
            self.current_state.update(state)
            self._last_position = float(self.current_state.get("position", 0.0))
            self._last_tick = time.monotonic()
        finally:
            await asyncio.sleep(0.08)
            self.apply_remote = False

    async def _send_state(self, reason: str) -> None:
        payload = {
            "type": "state_update",
            "reason": reason,
            "filename": self.current_state.get("filename", ""),
            "position": float(self.current_state.get("position", 0.0)),
            "paused": bool(self.current_state.get("paused", True)),
        }
        await self.send_server(payload)

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.apply_remote:
                continue
            if self.current_state.get("filename") and not self.current_state.get("paused", True):
                await self._send_state("heartbeat")

    async def mpv_listener(self) -> None:
        while True:
            line = await asyncio.wait_for(self.mpv.readline(), timeout=240)
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
            now = time.monotonic()

            if name == "pause" and isinstance(value, bool):
                self.current_state["paused"] = value
                self._last_tick = now
                self._last_position = float(self.current_state.get("position", 0.0))
                await self._send_state("pause" if value else "unpause")
            elif name == "time-pos" and isinstance(value, (float, int)):
                new_pos = float(value)
                prev_pos = float(self.current_state.get("position", 0.0))
                self.current_state["position"] = new_pos
                elapsed = max(0.0, now - self._last_tick)
                expected = self._last_position + (0.0 if self.current_state.get("paused", True) else elapsed)
                jump = abs(new_pos - expected)
                self._last_tick = now
                self._last_position = new_pos

                if self.current_state.get("paused", True) and abs(new_pos - prev_pos) > 0.05:
                    await self._send_state("seek")
                elif not self.current_state.get("paused", True) and jump > 1.5:
                    await self._send_state("seek")
            elif name == "path" and isinstance(value, str):
                self.current_state["filename"] = value
                await self._send_state("load")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Play-Con mpv sync client")
    p.add_argument("--room", required=True, help="Room name")
    p.add_argument("--pass", dest="password", required=True, help="Room password")
    p.add_argument("--mpv-path", default=DEFAULT_MPV_SOCKET, help="IPC path. On Windows, can also be path to mpv.exe")
    p.add_argument("--hide-ip", action="store_true", help="Hide printed server IP in header")
    p.add_argument("--server-ip", required=True, help="Server IP or hostname")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")
    p.add_argument("--tls", action="store_true", help="Use TLS and verify server certificate")
    p.add_argument(
        "--server-cert",
        default=str(DEFAULT_CERT_PATH),
        help=(
            "Path to trusted server certificate PEM (default: ~/.playcon_server_cert.pem). "
            "Required when --tls is used."
        ),
    )
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
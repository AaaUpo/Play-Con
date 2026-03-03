#!/usr/bin/env python3
import argparse
import asyncio
import contextlib
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
CERT_STORE = Path(__file__).resolve().parent / "server_cert.pem"


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
        self.mode = self._resolve_mode(args.mode)

        self.mpv_exec, self.mpv_ipc_path = self._resolve_mpv_launch_settings(args.mpv_path)
        self.mpv = MPVIPC(self.mpv_ipc_path)

        self.server_reader: Optional[asyncio.StreamReader] = None
        self.server_writer: Optional[asyncio.StreamWriter] = None
        self.current_state: Dict[str, Any] = {"filename": "", "position": 0.0, "paused": True}
        self.apply_remote = False
        self.mpv_process: Optional[asyncio.subprocess.Process] = None

        self._last_time_pos: Optional[float] = None
        self._last_time_pos_ts: Optional[float] = None
        self._last_position_push = 0.0
        self._last_server_revision = -1

    def _resolve_mode(self, mode: str) -> str:
        if mode != "auto":
            return mode
        target = self.server_ip.strip().lower()
        return "plaintext" if target in {"127.0.0.1", "localhost", "::1"} else "tls"

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
        print("=" * 72)
        print(f"Room      : {self.room}")
        print(f"Password  : {self.password}")
        print(f"Username  : {self.username}")
        print(f"Server IP : {ip_display}")
        print(f"Port      : {self.port}")
        print(f"Mode      : {self.mode.upper()}")
        print(f"TLS Cert  : {CERT_STORE}")
        print("=" * 72)

    async def establish_server_connection(self) -> None:
        if self.mode == "plaintext":
            self.server_reader, self.server_writer = await asyncio.wait_for(
                asyncio.open_connection(self.server_ip, self.port),
                timeout=8,
            )
            return

        if not CERT_STORE.exists():
            raise RuntimeError(
                "TLS mode selected but no trusted certificate found. "
                f"Expected certificate at: {CERT_STORE}. "
                "Copy the server certificate into this exact path and retry."
            )

        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=str(CERT_STORE))

        try:
            self.server_reader, self.server_writer = await asyncio.wait_for(
                asyncio.open_connection(self.server_ip, self.port, ssl=ctx, server_hostname="Play-Con-Server"),
                timeout=10,
            )
        except ssl.SSLError as exc:
            raise RuntimeError(
                "TLS handshake failed. Local server_cert.pem does not match the server certificate. "
                f"Path: {CERT_STORE}. Details: {exc}"
            ) from exc

    async def connect_and_join(self) -> Dict[str, Any]:
        await self.establish_server_connection()
        print(f"[client] Connected to server ({self.mode}).")
        await self.send_server(
            {
                "type": "join",
                "room": self.room,
                "password": self.password,
                "username": self.username,
            }
        )

        first = await self.read_server(timeout=10)
        if first.get("type") == "error":
            raise RuntimeError(f"Server rejected join: {first.get('message')}")
        if first.get("type") != "joined":
            raise RuntimeError(f"Unexpected first server reply: {first}")
        print(f"[server] Joined room {first.get('room')}")
        return first.get("state", {})

    async def start_mpv(self) -> None:
        if os.name != "nt":
            socket_path = Path(self.mpv_ipc_path)
            if socket_path.exists() and stat.S_ISSOCK(socket_path.stat().st_mode):
                socket_path.unlink(missing_ok=True)

        cmd = [
            self.mpv_exec,
            "--idle=yes",
            "--force-window=yes",
            f"--input-ipc-server={self.mpv_ipc_path}",
        ]
        print(f"[client] Launching mpv: {' '.join(cmd)}")
        self.mpv_process = await asyncio.create_subprocess_exec(*cmd)

        for _ in range(80):
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

    async def close_server(self) -> None:
        if self.server_writer:
            self.server_writer.close()
            with contextlib.suppress(Exception):
                await self.server_writer.wait_closed()
        self.server_writer = None
        self.server_reader = None

    async def run(self) -> None:
        await self.print_header()
        await self.start_mpv()

        mpv_task = asyncio.create_task(self.mpv_listener(), name="mpv_listener")
        mpv_watchdog = asyncio.create_task(self.wait_for_mpv_exit(), name="mpv_watchdog")
        try:
            await self.connection_loop(mpv_task, mpv_watchdog)
        finally:
            mpv_task.cancel()
            mpv_watchdog.cancel()
            await self.close_server()
            await self.shutdown_mpv()

    async def connection_loop(self, mpv_task: asyncio.Task, mpv_watchdog: asyncio.Task) -> None:
        backoff = 1.0
        while True:
            if mpv_task.done():
                await mpv_task
            if mpv_watchdog.done():
                await mpv_watchdog

            try:
                state = await self.connect_and_join()
                await self.apply_state_to_mpv(state)
                backoff = 1.0

                server_task = asyncio.create_task(self.server_listener(), name="server_listener")
                ping_task = asyncio.create_task(self.ping_loop(), name="ping_loop")

                done, pending = await asyncio.wait(
                    [server_task, ping_task, mpv_task, mpv_watchdog],
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc:
                        raise exc
            except Exception as exc:
                print(f"[client] Connection cycle ended: {exc}")
                await self.close_server()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 12.0)
                print(f"[client] Reconnecting in progress... (next retry delay cap: {backoff:.1f}s)")

    async def server_listener(self) -> None:
        while True:
            data = await self.read_server(timeout=240)
            msg_type = data.get("type")
            if msg_type == "error":
                print(f"[server:error] {data.get('message')}")
            elif msg_type == "state_update":
                await self.apply_state_to_mpv(data.get("state", {}))
            elif msg_type == "user_event":
                event = data.get("event")
                username = data.get("username", "unknown")
                verb = "joined" if event == "join" else "left" if event == "leave" else event
                print(f"[room] {username} {verb} the room")
            elif msg_type == "playback_event":
                event = data.get("event", "unknown")
                username = data.get("by", "unknown")
                filename = data.get("filename") or "(no file)"
                position = float(data.get("position", self.current_state.get("position", 0.0)))
                print(f"[playback] {username} {event} | file: {filename} | pos: {position:.3f}s")
            elif msg_type == "pong":
                pass
            else:
                print(f"[server] {data}")

    async def ping_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            await self.send_server({"type": "ping"})

    async def apply_state_to_mpv(self, state: Dict[str, Any]) -> None:
        if not self.mpv.is_connected():
            self.current_state.update(state)
            return

        incoming_revision = state.get("revision")
        if isinstance(incoming_revision, int):
            if incoming_revision <= self._last_server_revision:
                return
            self._last_server_revision = incoming_revision

        self.apply_remote = True
        try:
            filename = state.get("filename")
            file_changed = isinstance(filename, str) and filename and filename != self.current_state.get("filename")
            if file_changed:
                await self.mpv.command(["loadfile", filename, "replace"])
                self.current_state["filename"] = filename

            if "position" in state and self.current_state.get("filename"):
                pos = float(state["position"])
                local_pos = float(self.current_state.get("position", 0.0))
                paused_remote = bool(state.get("paused", self.current_state.get("paused", True)))
                drift = abs(local_pos - pos)

                # Avoid micro-seek jitter while playback is progressing naturally.
                should_seek = file_changed or paused_remote or drift > 0.85
                if should_seek:
                    await self.mpv.command(["set_property", "time-pos", pos])
                    self._last_time_pos = pos
                    self._last_time_pos_ts = time.monotonic()
                    self.current_state["position"] = pos

            if "paused" in state:
                paused = bool(state["paused"])
                await self.mpv.command(["set_property", "pause", paused])
                self.current_state["paused"] = paused
        finally:
            await asyncio.sleep(0.08)
            self.apply_remote = False

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
            if name == "pause" and isinstance(value, bool):
                if self.current_state.get("paused") != value:
                    self.current_state["paused"] = value
                    event = "pause" if value else "unpause"
                    await self.send_control_update(event=event)
            elif name == "path" and isinstance(value, str):
                if self.current_state.get("filename") != value:
                    self.current_state["filename"] = value
                    self.current_state["position"] = 0.0
                    await self.send_control_update(event="loadfile")
            elif name == "time-pos" and isinstance(value, (float, int)):
                current = float(value)
                now = time.monotonic()
                should_announce_seek = False
                if self._last_time_pos is not None and self._last_time_pos_ts is not None:
                    dt = now - self._last_time_pos_ts
                    expected = self._last_time_pos + (0.0 if self.current_state.get("paused", True) else dt)
                    if abs(current - expected) > 0.45:
                        should_announce_seek = True
                self._last_time_pos = current
                self._last_time_pos_ts = now
                self.current_state["position"] = current

                # Keep peers synced without flooding the server.
                if should_announce_seek:
                    await self.send_control_update(event="seek")
                elif now - self._last_position_push >= 1.0:
                    self._last_position_push = now
                    await self.send_control_update()

    async def send_control_update(self, event: Optional[str] = None) -> None:
        if not self.server_writer:
            return
        payload: Dict[str, Any] = {"type": "control_update", **self.current_state}
        if event:
            payload["event"] = event
        await self.send_server(payload)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Play-Con mpv sync client")
    p.add_argument("--room", required=True, help="Room name")
    p.add_argument("--pass", dest="password", required=True, help="Room password")
    p.add_argument("--mpv-path", default=DEFAULT_MPV_SOCKET, help="IPC path. On Windows, can also be path to mpv.exe")
    p.add_argument("--hide-ip", action="store_true", help="Hide printed server IP in header")
    p.add_argument("--server-ip", required=True, help="Server IP or hostname")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port to connect to (default: {DEFAULT_PORT})")
    p.add_argument(
        "--mode",
        choices=["auto", "plaintext", "tls"],
        default="auto",
        help="Connection mode. auto=plaintext for localhost else tls (default: auto)",
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

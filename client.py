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
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_PORT = 3232
DEFAULT_TLS_PORT = 3233
DEFAULT_MPV_SOCKET = "/tmp/mpv-socket"
DEFAULT_MPV_PIPE = r"\\.\pipe\playcon-mpv"
CERT_STORE = Path.home() / ".playcon_server_cert.pem"


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
        self.tls_port = args.tls_port

        self.mpv_exec, self.mpv_ipc_path = self._resolve_mpv_launch_settings(args.mpv_path)
        self.mpv = MPVIPC(self.mpv_ipc_path)

        self.server_reader: Optional[asyncio.StreamReader] = None
        self.server_writer: Optional[asyncio.StreamWriter] = None
        self.current_state: Dict[str, Any] = {"filename": "", "position": 0.0, "paused": True}
        self.apply_remote = False
        self.mpv_process: Optional[asyncio.subprocess.Process] = None

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
        target_port = self.port if self._is_local_target() else self.tls_port
        print("=" * 72)
        print(f"Room      : {self.room}")
        print(f"Password  : {self.password}")
        print(f"Username  : {self.username}")
        print(f"Server IP : {ip_display}")
        print(f"Port      : {target_port}")
        print("=" * 72)

    def _is_local_target(self) -> bool:
        target = self.server_ip.strip().lower()
        return target in {"127.0.0.1", "localhost", "::1"}

    async def _open_plain(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(asyncio.open_connection(self.server_ip, self.port), timeout=8)

    async def _open_tls(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(self._connect_tls_with_validation(), timeout=10)

    async def establish_server_connection(self) -> None:
        if self._is_local_target():
            print("[client] Localhost target detected: using plaintext only (TLS disabled).")
            try:
                self.server_reader, self.server_writer = await self._open_plain()
            except OSError as exc:
                raise RuntimeError(
                    f"Could not connect to local server at {self.server_ip}:{self.port} over plaintext ({exc}). "
                    "Check the local server --port and firewall settings."
                ) from exc
            print("[client] Using plaintext mode.")
            return

        try:
            self.server_reader, self.server_writer = await self._open_tls()
        except OSError as exc:
            details = [
                f"Could not connect to remote server at {self.server_ip}:{self.tls_port} over TLS ({exc}).",
                "Check server is running, port forwarding/firewall, and matching --tls-port.",
            ]
            if isinstance(exc, ConnectionRefusedError) or getattr(exc, "errno", None) in {
                errno.ECONNREFUSED,
                getattr(errno, "WSAECONNREFUSED", 10061),
            }:
                details.append(
                    "If client and server are on the same machine/network, do not use the public IP from inside the LAN: "
                    "use --server-ip 127.0.0.1 (same PC) or the server's LAN IP (e.g. 192.168.x.x). "
                    "Many routers/ISPs block NAT loopback (hairpin), which causes this exact refusal."
                )
            raise RuntimeError(" ".join(details)) from exc
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

        try:
            return await asyncio.open_connection(self.server_ip, self.tls_port, ssl=make_ctx(), server_hostname="Play-Con-Server")
        except ssl.SSLCertVerificationError as exc:
            print(f"[client] Stored cert invalid or changed: {exc}")
            if not await self._prompt_trust_and_store_cert():
                raise RuntimeError("User rejected new server certificate") from exc
            return await asyncio.open_connection(self.server_ip, self.tls_port, ssl=make_ctx(), server_hostname="Play-Con-Server")

    async def _prompt_trust_and_store_cert(self) -> bool:
        pem = await asyncio.to_thread(ssl.get_server_certificate, (self.server_ip, self.tls_port))
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
        if not self.mpv.is_connected():
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
    p.add_argument("--mpv-path", default=DEFAULT_MPV_SOCKET, help="IPC path. On Windows, can also be path to mpv.exe")
    p.add_argument("--hide-ip", action="store_true", help="Hide printed server IP in header")
    p.add_argument("--server-ip", required=True, help="Server IP or hostname")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Plaintext local port (default: {DEFAULT_PORT})")
    p.add_argument("--tls-port", type=int, default=DEFAULT_TLS_PORT, help=f"TLS remote port (default: {DEFAULT_TLS_PORT})")
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
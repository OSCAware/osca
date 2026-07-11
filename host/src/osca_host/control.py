"""控制通道：unix socket 上的 JSON-lines 协议。

启停是运行时对注册表的操作——操作入口就是这条通道。
每行一个 JSON 请求 {"cmd": ...}，回一行 JSON 响应 {"ok": ...}。
命令：status / load / unload / stop。协议保持傻：本地管控，不是 API。
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

DEFAULT_SOCKET = Path.home() / ".osca" / "host.sock"


class ControlServer:
    """挂在 Host 事件循环上的控制端。handler 由 Host 注入。"""

    def __init__(self, socket_path: Path, handler):
        self.socket_path = socket_path
        self.handler = handler  # dict 请求 → dict 响应（同步，注册表操作都很快）
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.socket_path))

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if line:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    response = {"ok": False, "detail": "请求不是合法 JSON"}
                else:
                    response = self.handler(request)
                writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()


def send_command(request: dict, socket_path: Path = DEFAULT_SOCKET, timeout: float = 30.0) -> dict:
    """客户端：发一条命令，等一行响应。Host 未运行时给人话报错。"""
    if not socket_path.exists():
        return {"ok": False, "detail": f"Host 未运行（控制通道不存在：{socket_path}）"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf) if buf.strip() else {"ok": False, "detail": "Host 未响应"}

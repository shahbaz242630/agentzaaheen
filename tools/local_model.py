"""Run the local model trial's model server on this laptop (F05): Gemma via llama.cpp.

Starts ``llama-server`` on http://127.0.0.1:8766 only (never the network), with
its own web page switched off, one request at a time, and thinking off so a
laptop CPU answers in seconds, not minutes. It first checks there is enough
free memory, and refuses rather than squeeze other work on the laptop. Stop
it with Ctrl+C; nothing keeps running afterwards.

The dashboard then talks to it through the core's model client:

    uv run python -m tools.local_model                       # terminal 1
    uv run python -m tools.dashboard --model-url http://127.0.0.1:8766   # terminal 2

The server binary and model live outside the repo, under
``%LOCALAPPDATA%/Yaqzan/llm`` (checksum-verified when downloaded).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

HOST = "127.0.0.1"
DEFAULT_PORT = 8766
LLM_HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Yaqzan" / "llm"
DEFAULT_SERVER = (
    LLM_HOME / "llama-b11624" / ("llama-server.exe" if sys.platform == "win32" else "llama-server")
)
DEFAULT_MODEL = LLM_HOME / "models" / "gemma-4-E2B_q4_0-it.gguf"
# The model file plus room for its working memory (context, buffers) and the rest of the laptop.
HEADROOM_BYTES = 1536 * 1024 * 1024
CONTEXT_TOKENS = 4096
START_TIMEOUT_S = 180.0
GIB = 1024**3


def free_memory_bytes() -> int | None:
    """Physical memory free right now, or ``None`` if this system will not say."""
    if sys.platform == "win32":

        class MemoryStatus(ctypes.Structure):
            _fields_ = (  # ctypes needs this exact class attribute
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            )

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined,unused-ignore]  # windll exists on Windows only
            return None
        return int(status.ullAvailPhys)
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def server_command(server: Path, model: Path, port: int) -> list[str]:
    """The exact llama-server command: loopback only, no web page, one slot, thinking off."""
    return [
        str(server),
        "--model",
        str(model),
        "--host",
        HOST,
        "--port",
        str(port),
        "--no-webui",
        "--parallel",
        "1",
        "--ctx-size",
        str(CONTEXT_TOKENS),
        "--reasoning",
        "off",
    ]


def memory_problem(model: Path, free: int | None) -> str | None:
    """Why there is not enough memory to start, or ``None`` if there is."""
    if free is None:
        return "could not tell how much memory is free, so the model was not started."
    needed = model.stat().st_size + HEADROOM_BYTES
    if free < needed:
        return (
            f"only {free / GIB:.1f} GB of memory is free and the model needs about {needed / GIB:.1f} GB. "
            "Try again when the laptop is less busy (other work is not stopped to make room)."
        )
    return None


def wait_until_ready(
    port: int,
    process: subprocess.Popen[bytes],
    *,
    get: Callable[[str], int],
    timeout_s: float = START_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll ``/health`` until the model is loaded, the server exits, or time runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            if get(f"http://{HOST}:{port}/health") == 200:
                return True
        except httpx.HTTPError:
            pass
        sleep(1.0)
    return False


def _get_status(url: str) -> int:  # pragma: no cover - talks to the real local server
    return httpx.get(url, timeout=2.0, follow_redirects=False, trust_env=False).status_code


def main(  # noqa: PLR0913  # the real world (memory, processes, health checks) is injectable for tests
    argv: Sequence[str] | None = None,
    *,
    free_memory: Callable[[], int | None] = free_memory_bytes,
    start: Callable[[list[str]], subprocess.Popen[bytes]] | None = None,
    get: Callable[[str], int] = _get_status,
    timeout_s: float = START_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    block: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> int:
    """Start the model server and keep it running until Ctrl+C; return 1 if it cannot start."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--server", type=Path, default=DEFAULT_SERVER, help="path to llama-server")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="path to the .gguf model")
    ns = parser.parse_args(argv)
    if not 1024 <= ns.port <= 65535:
        print("local model: pick a port between 1024 and 65535.")
        return 1
    for what, path in (("llama-server", ns.server), ("model", ns.model)):
        if not path.is_file():
            print(f"local model: no {what} at {path}.")
            return 1
    problem = memory_problem(ns.model, free_memory())
    if problem is not None:
        print(f"local model: {problem}")
        return 1
    print(f"local model: loading {ns.model.name} (this can take a minute)...")
    process = (start or _start)(server_command(ns.server, ns.model, ns.port))
    try:
        if not wait_until_ready(ns.port, process, get=get, timeout_s=timeout_s, sleep=sleep):
            print("local model: the server did not become ready; it has been stopped.")
            return 1
        print(f"local model: ready at http://{HOST}:{ns.port} (press Ctrl+C to stop)")
        (block or _block)(process)
    except KeyboardInterrupt:
        pass
    finally:
        _stop(process)
    return 0


def _start(command: list[str]) -> subprocess.Popen[bytes]:  # pragma: no cover - starts a real process
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603


def _block(process: subprocess.Popen[bytes]) -> None:  # pragma: no cover - waits on a real process
    process.wait()


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    sys.exit(main())

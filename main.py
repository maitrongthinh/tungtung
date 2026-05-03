from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from threading import Thread

PROJECT_ROOT = Path(__file__).resolve().parent
BOOTSTRAP_ENV_FLAG = "SHOPEE_AGENT_BOOTSTRAPPED"
REEXEC_311_ENV_FLAG = "SHOPEE_AGENT_REEXEC_311"
REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "langgraph",
    "playwright",
    "sse_starlette",
    "chromadb",
)


def handoff_to_python311_if_available() -> None:
    if __name__ != "__main__":
        return
    if sys.version_info[:2] == (3, 11):
        return
    if os.environ.get(REEXEC_311_ENV_FLAG) == "1":
        return
    try:
        result = subprocess.run(
            ["py", "-3.11", "-c", "import sys; print(sys.executable)"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return
    target = result.stdout.strip()
    if not target:
        return
    print(f"[launcher] Current Python is {sys.version.split()[0]}. Handoff to Python 3.11: {target}", flush=True)
    env = os.environ.copy()
    env[REEXEC_311_ENV_FLAG] = "1"
    raise SystemExit(
        subprocess.call(
            ["py", "-3.11", str(PROJECT_ROOT / "main.py"), *sys.argv[1:]],
            cwd=PROJECT_ROOT,
            env=env,
        )
    )


def ensure_runtime_ready() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11+ is required")
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if not missing:
        return
    if os.environ.get(BOOTSTRAP_ENV_FLAG) == "1":
        raise RuntimeError(f"Auto-bootstrap failed, still missing modules: {', '.join(missing)}")
    print(f"[bootstrap] Missing modules detected: {', '.join(missing)}", flush=True)

    # Trên Linux/Debian mới: Python system bị externally-managed, phải dùng venv
    venv_dir = PROJECT_ROOT / ".venv"
    if sys.platform != "win32":
        venv_python = venv_dir / "bin" / "python"
        venv_pip = venv_dir / "bin" / "pip"
        # Tạo venv nếu chưa có
        if not venv_dir.exists():
            print(f"[bootstrap] Creating virtualenv at {venv_dir} ...", flush=True)
            subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
        # Cài dependencies vào venv (dù đang chạy từ system hay venv)
        print(f"[bootstrap] Installing dependencies into venv ...", flush=True)
        subprocess.check_call([str(venv_pip), "install", "-e", ".[dev]"], cwd=PROJECT_ROOT)
        # Re-exec từ venv python (không set BOOTSTRAP flag để main logic chạy bình thường)
        print(f"[bootstrap] Re-executing inside venv: {venv_python}", flush=True)
        raise SystemExit(
            subprocess.call(
                [str(venv_python), str(PROJECT_ROOT / "main.py"), *sys.argv[1:]],
                cwd=PROJECT_ROOT,
            )
        )

    # Windows: pip install thẳng
    print(f"[bootstrap] Installing dependencies into: {sys.executable}", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], cwd=PROJECT_ROOT)
    env = os.environ.copy()
    env[BOOTSTRAP_ENV_FLAG] = "1"
    os.execve(sys.executable, [sys.executable, str(PROJECT_ROOT / "main.py")], env)


handoff_to_python311_if_available()
if __name__ == "__main__":
    ensure_runtime_ready()

import uvicorn  # noqa: E402

from common.config import load_settings  # noqa: E402
from common.logging import get_logger  # noqa: E402
from core.bootstrap import build_runtime  # noqa: E402
from core.loop_controller import DailyLoopController  # noqa: E402
from core.scheduler import AgentScheduler  # noqa: E402
from web.main import app  # noqa: E402

logger = get_logger(__name__)


class WebServerThread(Thread):
    def __init__(self) -> None:
        super().__init__(name="web-server", daemon=True)
        settings = load_settings(refresh=True)
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.web.host,
                port=settings.web.port,
                reload=False,
                log_level=settings.web.log_level,
            )
        )

    def run(self) -> None:
        logger.info("Starting web server on %s:%s", self.server.config.host, self.server.config.port)
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def main() -> None:
    runtime = build_runtime()
    scheduler = AgentScheduler(
        database=runtime.database,
        session_manager=runtime.session_manager,
        proxy_pool=runtime.proxy_pool,
    )
    controller = DailyLoopController(runtime.database, scheduler)
    web_server = WebServerThread()
    web_server.start()
    try:
        asyncio.run(controller.run_forever())
    finally:
        web_server.stop()
        web_server.join(timeout=15)
        logger.info("Unified application stopped")


if __name__ == "__main__":
    main()

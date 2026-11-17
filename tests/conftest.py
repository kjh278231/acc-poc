import asyncio
import os
import socket
import threading
import time

import pytest

os.environ["LLM_MODE"] = "mock"
os.environ["A2A_API_KEY"] = "test-key"
os.environ["TMS_LATENCY_MS"] = "5"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.factory import build_orchestrator  # noqa: E402
from app.services.llm_mock import MockLLM  # noqa: E402
from app.services.tms_mock import TmsMock  # noqa: E402


@pytest.fixture
def orch():
    return build_orchestrator(llm=MockLLM(), tms=TmsMock(latency_ms=5))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server():
    """Real uvicorn server on a free port (A2A over actual HTTP/WebSocket)."""
    import uvicorn

    from app.main import create_app

    o = build_orchestrator(llm=MockLLM(haiku_latency_s=0.02, jev_latency_s=0.01), tms=TmsMock(latency_ms=5))
    app = create_app(o)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield {"base": f"http://127.0.0.1:{port}", "ws": f"ws://127.0.0.1:{port}", "app": app, "orch": o}
    srv.should_exit = True
    th.join(timeout=5)

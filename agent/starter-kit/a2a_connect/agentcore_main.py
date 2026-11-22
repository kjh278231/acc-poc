# SPDX-License-Identifier: MIT-0
"""Entry point for an Amazon Bedrock AgentCore Runtime deployment.

AgentCore direct code deployment runs ``python -m a2a_connect.agentcore_main``
(or imports :func:`run`). It starts the kit in the AgentCore HTTP contract with
the echo backend and AgentCore's inbound auth as the gate -- equivalent to::

    python -m a2a_connect.server --agentcore http --auth agentcore \
        --backend echo

The mode, backend and auth can be overridden with the environment variables
below, so the same image can serve the A2A contract or the bedrock backend
without a rebuild.

Environment:
  A2A_AGENTCORE_MODE   ``http`` (default) or ``a2a``
  A2A_BACKEND          ``echo`` (default) or ``bedrock``
  A2A_AGENT_NAME       root-span agent name (default ``a2a-ws-spike``)
"""
from __future__ import annotations

import os

from .server import main


def run() -> int:
    mode = os.environ.get("A2A_AGENTCORE_MODE", "http")
    backend = os.environ.get("A2A_BACKEND", "echo")
    agent_name = os.environ.get("A2A_AGENT_NAME", "a2a-ws-spike")
    argv = [
        "--agentcore", mode,
        "--auth", "agentcore",
        "--backend", backend,
        "--agent-name", agent_name,
    ]
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(run())

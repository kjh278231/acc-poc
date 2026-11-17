"""Engine interface. Engines only interpret and propose; the rule engine and orchestrator act."""
from __future__ import annotations

from typing import Any, Protocol

from app.core.decision import Decision, ModelCall
from app.core.state import Action, CallState


class Engine(Protocol):
    name: str
    parallel_tools: bool  # True: speak while the tool runs. False: tool first, then generate the reply.

    async def decide(self, state: CallState, text: str) -> Decision: ...

    async def compose_after_tool(self, state: CallState, action: Action, tool_result: dict[str, Any],
                                 draft: str | None) -> tuple[str | None, list[ModelCall]]:
        """Final reply once a tool has run. None -> use the template."""
        ...

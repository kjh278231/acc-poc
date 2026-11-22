# SPDX-License-Identifier: MIT-0
"""A reference server for the Amazon Connect Customer A2A extension (text only).

This package implements the public A2A protocol contract for external AI agents:
the bearer check and extension echo, INIT_SESSION, channel readiness, the turn
lifecycle (text response artifacts, one TRACING_SPAN per turn, one terminal
status), FINISH, interruptions, DTMF, silence, fragmentation, and structured
task failures, over both the WebSocket and HTTPS JSON-RPC transports.

The kit owns every protocol frame and the order it sends them in. A backend only
yields semantic events (text, tool calls, model calls, an optional finish); it
cannot get the frame order wrong.
"""

EXT = "https://docs.aws.amazon.com/connect/a2a/ext/v1"
EVENT_TYPE_KEY = EXT + "/eventType"

__all__ = ["EXT", "EVENT_TYPE_KEY"]

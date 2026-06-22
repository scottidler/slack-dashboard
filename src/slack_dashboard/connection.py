import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ConnectionState:
    """Live Socket Mode connection state, shared between the socket monitor and the web UI.

    Drives the trust banner: zero-miss holds live only while Socket Mode is connected.
    When it drops, the banner warns that the view may be missing recent activity; on
    reconnect the poller reconciles the gap.
    """

    socket_enabled: bool = False
    connected: bool = False

    def status(self) -> str:
        """One of: ``disabled`` (no app token), ``connected``, ``disconnected``."""
        if not self.socket_enabled:
            return "disabled"
        return "connected" if self.connected else "disconnected"

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ConnectionState:
    """Live Socket Mode connection state, shared between the socket monitor and the web UI.

    Drives the trust banner: zero-miss holds live only while Socket Mode is connected.
    When it drops, the banner warns that the view may be missing recent activity; on
    reconnect the poller reconciles the gap.

    The disconnect edge (``mark_disconnected``, fired from slack_sdk's on_close listener)
    and the reconnect edge (``observe``, fired from the is_connected() poll) compose through
    ``reconcile_pending``. Because the on_close edge sets the flag, a disconnect+reconnect
    that happens entirely between two polls is still caught: the next ``observe(True)`` sees
    the pending flag and triggers a reconcile. Without this, a sub-poll reconnect would
    silently skip catch-up.
    """

    socket_enabled: bool = False
    connected: bool = False
    reconcile_pending: bool = False

    def status(self) -> str:
        """One of: ``disabled`` (no app token), ``connected``, ``disconnected``."""
        if not self.socket_enabled:
            return "disabled"
        return "connected" if self.connected else "disconnected"

    def mark_disconnected(self) -> None:
        """Record a disconnect (from on_close) and arm a reconcile for the next reconnect."""
        logger.debug("ConnectionState.mark_disconnected: arming reconcile")
        self.connected = False
        self.reconcile_pending = True

    def observe(self, connected: bool) -> bool:
        """Update state from a connection poll; return True iff a reconcile should run now.

        A reconcile is due when we are connected and a disconnect has been pending since the
        last successful reconcile. Clears the pending flag when it fires so reconcile runs
        once per disconnect, not once per poll.
        """
        self.connected = connected
        if connected and self.reconcile_pending:
            self.reconcile_pending = False
            logger.debug(
                "ConnectionState.observe: reconnect with pending reconcile -> reconcile now"
            )
            return True
        return False

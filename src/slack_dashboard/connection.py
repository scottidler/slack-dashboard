import asyncio
import logging
from collections.abc import Awaitable, Callable
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


async def monitor_connection(
    is_connected: Callable[[], Awaitable[bool]],
    connection: ConnectionState,
    reconcile: Callable[[], Awaitable[None]],
    *,
    interval: float = 5.0,
) -> None:
    """Poll the live connection and reconcile on the reconnect edge.

    slack_sdk has no on-connect callback, so we poll ``is_connected()`` (an async method)
    and let ``ConnectionState.observe`` decide when a reconcile is due (it composes with the
    on_close-driven ``mark_disconnected``). Each iteration is guarded so a transient
    ``is_connected()`` error logs and continues rather than killing the monitor, which
    would silence all future catch-up. Returns on cancellation (clean shutdown).
    """
    logger.debug("monitor_connection: starting (interval=%.1fs)", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                if connection.observe(bool(await is_connected())):
                    logger.info("Socket Mode reconnected; reconciling missed activity")
                    await reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("connection monitor iteration failed; continuing")
    except asyncio.CancelledError:
        return

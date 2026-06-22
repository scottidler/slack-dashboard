import pytest

from slack_dashboard.connection import ConnectionState


def test_status_disabled_when_no_socket() -> None:
    state = ConnectionState(socket_enabled=False)
    assert state.status() == "disabled"


def test_status_connected() -> None:
    state = ConnectionState(socket_enabled=True, connected=True)
    assert state.status() == "connected"


def test_status_disconnected() -> None:
    state = ConnectionState(socket_enabled=True, connected=False)
    assert state.status() == "disconnected"


def test_observe_no_reconcile_without_disconnect() -> None:
    state = ConnectionState(socket_enabled=True, connected=True)
    assert state.observe(True) is False


def test_short_disconnect_still_triggers_reconcile() -> None:
    # The race: disconnect + reconnect happen entirely between two polls, so the poll only
    # ever sees connected=True. The on_close edge armed reconcile, so it must still fire.
    state = ConnectionState(socket_enabled=True, connected=True)
    state.mark_disconnected()  # on_close fired between polls
    assert state.observe(True) is True
    assert state.reconcile_pending is False


def test_reconcile_fires_once_per_disconnect() -> None:
    state = ConnectionState(socket_enabled=True, connected=True)
    state.mark_disconnected()
    assert state.observe(True) is True
    # No further reconcile until the next disconnect
    assert state.observe(True) is False


def test_pending_survives_polls_while_disconnected() -> None:
    state = ConnectionState(socket_enabled=True, connected=True)
    state.mark_disconnected()
    # Still down across several polls; reconcile stays armed until we see connected again
    assert state.observe(False) is False
    assert state.observe(False) is False
    assert state.reconcile_pending is True
    assert state.observe(True) is True


def test_mark_disconnected_sets_status() -> None:
    state = ConnectionState(socket_enabled=True, connected=True)
    state.mark_disconnected()
    assert state.status() == "disconnected"


@pytest.mark.asyncio
async def test_monitor_reconciles_on_reconnect_edge() -> None:
    import asyncio
    import contextlib

    from slack_dashboard.connection import monitor_connection

    state = ConnectionState(socket_enabled=True, connected=True)
    state.mark_disconnected()  # on_close fired
    seq = iter([False, True, True])

    async def is_connected() -> bool:
        return next(seq, True)

    calls: list[int] = []

    async def reconcile() -> None:
        calls.append(1)

    task = asyncio.create_task(monitor_connection(is_connected, state, reconcile, interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert calls == [1]  # reconciled exactly once on the reconnect edge


@pytest.mark.asyncio
async def test_monitor_survives_is_connected_error() -> None:
    import asyncio
    import contextlib

    from slack_dashboard.connection import monitor_connection

    state = ConnectionState(socket_enabled=True, connected=True)
    state.mark_disconnected()
    calls: list[int] = []
    step = {"n": 0}

    async def is_connected() -> bool:
        step["n"] += 1
        if step["n"] == 1:
            raise RuntimeError("boom")  # transient failure on first poll
        return True

    async def reconcile() -> None:
        calls.append(1)

    task = asyncio.create_task(monitor_connection(is_connected, state, reconcile, interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # The loop survived the exception and still reconciled on a later poll
    assert calls == [1]

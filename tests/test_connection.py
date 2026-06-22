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

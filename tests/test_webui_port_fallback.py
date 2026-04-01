import socket

import webui


def _occupy_tcp_port(host: str) -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind((host, 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _find_free_tcp_port(host: str) -> int:
    sock, port = _occupy_tcp_port(host)
    sock.close()
    return port


def test_select_webui_port_keeps_requested_port_when_available():
    requested_port = _find_free_tcp_port("127.0.0.1")
    port, switched = webui._select_webui_port("127.0.0.1", requested_port)

    assert port == requested_port
    assert switched is False


def test_select_webui_port_switches_when_requested_port_is_occupied():
    occupied_sock, occupied_port = _occupy_tcp_port("127.0.0.1")

    try:
        selected_port, switched = webui._select_webui_port("127.0.0.1", occupied_port)
    finally:
        occupied_sock.close()

    assert switched is True
    assert selected_port != occupied_port
    assert selected_port > 0


def test_format_access_host_maps_wildcard_host_to_loopback():
    assert webui._format_access_host("0.0.0.0") == "127.0.0.1"
    assert webui._format_access_host("::") == "[::1]"
    assert webui._format_access_host("127.0.0.1") == "127.0.0.1"

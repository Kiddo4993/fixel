import io
import struct

import pytest
from rich.console import Console


def _make_png(width: int, height: int) -> bytes:
    """Just enough of a PNG for size sniffing (signature + IHDR)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


@pytest.fixture
def png():
    return _make_png


@pytest.fixture
def console():
    return Console(file=io.StringIO(), width=100, force_terminal=False, color_system=None)


@pytest.fixture(autouse=True)
def no_real_keys_or_network(monkeypatch):
    """No test may read or overwrite the real Keychain, or reach a paid API.

    Keys live in a throwaway dict, API-key environment variables are hidden, and any connection that
    leaves this Mac fails (the playground tests talk to 127.0.0.1, which stays allowed).
    """
    import socket

    import keyring

    from fixel import config

    vault: dict[tuple[str, str], str] = {}

    def delete(service, user):
        if vault.pop((service, user), None) is None:
            raise keyring.errors.PasswordDeleteError(user)

    monkeypatch.setattr(keyring, "get_password", lambda service, user: vault.get((service, user)))
    monkeypatch.setattr(keyring, "set_password", lambda service, user, pw: vault.__setitem__((service, user), pw))
    monkeypatch.setattr(keyring, "delete_password", delete)
    for name in config.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FIXEL_HOME", raising=False)

    real_connect = socket.socket.connect

    def local_only(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and sock.family != socket.AF_UNIX:
            raise OSError(f"tests may not reach the network ({host})")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)

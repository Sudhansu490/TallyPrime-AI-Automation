"""Bind-address policy and the production launcher. No real server is started."""

import socket
from pathlib import Path

import pytest

from tally_automation import server
from tally_automation.config import Environment, Settings
from tally_automation.server import BindHostError, validate_bind_host


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "100.64.0.1", "100.95.249.91", "100.127.255.254",
     "fd7a:115c:a1e0::1"],
)
def test_loopback_and_tailscale_addresses_are_accepted(host: str) -> None:
    validate_bind_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0", "::",                    # wildcard: every interface
        "192.168.1.51", "10.0.0.5",         # LAN
        "8.8.8.8", "2001:4860::8888",       # public
        "100.63.255.255", "100.128.0.1",    # just outside 100.64.0.0/10
        "localhost", "computer-a.ts.net",   # hostnames may resolve anywhere
        "",
    ],
)
def test_other_addresses_are_rejected(host: str) -> None:
    with pytest.raises(BindHostError):
        validate_bind_host(host)


@pytest.fixture
def launcher_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Keep the launcher from reconfiguring global logging during tests.
    monkeypatch.setattr(server, "configure_logging", lambda *args, **kwargs: None)
    return Settings(
        _env_file=None,
        app_env=Environment.TEST,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_run_refuses_wildcard_before_binding(launcher_settings: Settings, monkeypatch) -> None:
    def must_not_bind(*_args: object) -> None:
        raise AssertionError("no socket should be bound")

    monkeypatch.setattr(server, "_bind_socket", must_not_bind)
    settings = launcher_settings.model_copy(update={"bind_hosts": ["127.0.0.1", "0.0.0.0"]})
    with pytest.raises(BindHostError):
        server.run(settings)


def test_run_binds_exactly_the_configured_addresses(launcher_settings: Settings, monkeypatch) -> None:
    captured: list[tuple[str, int]] = []

    def fake_run(self: object, sockets: list[socket.socket]) -> None:
        captured.extend(sock.getsockname()[:2] for sock in sockets)
        for sock in sockets:
            sock.close()

    monkeypatch.setattr(server.uvicorn.Server, "run", fake_run)
    # The schema guard is tested in test_operations; here the database is not the subject.
    monkeypatch.setattr("tally_automation.operations.schema_status", lambda settings: ("head", "head"))
    port = _free_port()
    server.run(launcher_settings.model_copy(update={"app_port": port}))
    assert captured == [("127.0.0.1", port)]


def test_run_exits_cleanly_when_port_is_taken(launcher_settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(server.uvicorn.Server, "run", lambda *a, **k: pytest.fail("server must not start"))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        with pytest.raises(SystemExit) as excinfo:
            server.run(launcher_settings.model_copy(update={"app_port": port}))
    assert excinfo.value.code == 1

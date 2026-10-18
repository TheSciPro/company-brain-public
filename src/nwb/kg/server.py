"""A local FalkorDB server on TCP, started from the binaries that ship in the pinned ``falkordblite`` wheel.

GraphRAG-SDK and Graphiti both connect by host/port, while FalkorDBLite's embedded mode uses a unix socket. Running
the same ``redis-server`` + ``falkordb.so`` on a loopback port gives both libraries one real server with nothing
installed system-wide. Production points at FalkorDB Cloud (``FALKORDB_URL``) instead.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from urllib.parse import unquote, urlparse

from nwb.conf.models import GraphServerConfig
from nwb.config import Settings
from nwb.errors import ConfigError


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


@dataclass
class LocalFalkorServer:
    """``with LocalFalkorServer(data_dir) as srv: srv.port``. Persistence off unless ``persist`` is set."""

    data_dir: Path
    port: int = 0
    persist: bool = False
    host: str = "127.0.0.1"
    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)

    @staticmethod
    def binaries() -> tuple[Path, Path]:
        import redislite

        server = Path(redislite.__redis_executable__)
        module = Path(redislite.__falkordb_module__)
        if not server.exists() or not module.exists():
            raise ConfigError("falkordblite binaries not found; reinstall falkordblite")
        return server, module

    def start(self, timeout_s: float = 10.0) -> LocalFalkorServer:
        if self._proc is not None:
            return self
        server, module = self.binaries()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.port = self.port or _free_port()
        args = [
            str(server),
            "--bind",
            self.host,
            "--port",
            str(self.port),
            "--dir",
            str(self.data_dir),
            "--loadmodule",
            str(module),
            "--appendonly",
            "no",
            "--protected-mode",
            "yes",
        ]
        args += ["--save", "60 1"] if self.persist else ["--save", ""]
        log = (self.data_dir / "falkordb.log").open("ab")
        self._proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603 - fixed binary path
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if _port_open(self.host, self.port):
                return self
            if self._proc.poll() is not None:
                break
            time.sleep(0.05)
        self.stop()
        raise ConfigError(f"local FalkorDB did not start on {self.host}:{self.port}; see {self.data_dir}/falkordb.log")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    def wipe(self) -> None:
        self.stop()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def __enter__(self) -> LocalFalkorServer:
        return self.start()

    def __exit__(self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None) -> None:
        self.stop()


@dataclass(frozen=True)
class FalkorEndpoint:
    """Where both graph libraries connect. Parsed from ``FALKORDB_URL`` (``redis://user:pass@host:port``)."""

    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ssl: bool = False

    @classmethod
    def from_url(cls, url: str) -> FalkorEndpoint:
        u = urlparse(url)
        if u.scheme not in {"redis", "rediss", "falkor", "falkors"} or not u.hostname or not u.port:
            raise ConfigError("FALKORDB_URL must look like redis://user:pass@host:port")
        return cls(
            host=u.hostname,
            port=u.port,
            username=unquote(u.username) if u.username else None,
            password=unquote(u.password) if u.password else None,
            ssl=u.scheme in {"rediss", "falkors"},
        )

    @property
    def is_local(self) -> bool:
        return self.host in {"127.0.0.1", "localhost"}


@contextmanager
def falkor_endpoint(url: str | None, local_dir: Path, persist: bool = False) -> Iterator[FalkorEndpoint]:
    """Cloud when ``url`` is set, otherwise a server whose files live in ``local_dir``.

    Callers that want the company-graph directory (``graph.yaml`` ``server.local_dir``)
    use ``resolve_company_falkor`` instead of inventing a temp directory.
    """
    if url:
        yield FalkorEndpoint.from_url(url)
        return
    with LocalFalkorServer(local_dir, persist=persist) as srv:
        yield FalkorEndpoint(host=srv.host, port=srv.port)


@dataclass(frozen=True)
class CompanyFalkorTarget:
    """The server company graphs are written to and read from.

    ``endpoint`` is a server that is already running. ``local_dir`` is the on-disk
    fallback; the caller starts ``LocalFalkorServer`` there and leaves the files
    in place. One of the two is set.
    """

    endpoint: FalkorEndpoint | None = None
    local_dir: Path | None = None
    local_port: int = 0
    persist: bool = True

    @property
    def is_local(self) -> bool:
        return self.local_dir is not None


def resolve_company_falkor(
    settings: Settings,
    server: GraphServerConfig,
    *,
    force_local: bool = False,
) -> CompanyFalkorTarget:
    """Where a company-graph load and a company-graph search connect.

    ``FALKORDB_URL`` wins, then ``Settings.falkor_target()`` when it names a host.
    Otherwise both sides use ``server.local_dir`` — not a throwaway temp directory,
    and not the embedded unix-socket store. ``force_local`` is the loader's
    ``--local`` flag: it keeps that directory even when a URL is set.
    """
    if not force_local and settings.falkordb_url:
        return CompanyFalkorTarget(endpoint=FalkorEndpoint.from_url(settings.falkordb_url))
    remote = settings.falkor_target()
    if not force_local and remote.host:
        password = remote.password.get_secret_value() if remote.password else None
        return CompanyFalkorTarget(
            endpoint=FalkorEndpoint(
                host=remote.host,
                port=remote.port,
                username=remote.username,
                password=password,
            )
        )
    return CompanyFalkorTarget(
        local_dir=Path(server.local_dir),
        local_port=server.local_port,
        persist=server.persist,
    )


@contextmanager
def open_company_falkor(target: CompanyFalkorTarget) -> Iterator[FalkorEndpoint]:
    """Connect to ``target``. A local directory is started, persisted, and stopped on exit.

    Stopping the process does not delete ``local_dir``, so the next reader of the
    same target sees the load.
    """
    if target.endpoint is not None:
        yield target.endpoint
        return
    if target.local_dir is None:
        raise ConfigError("company FalkorDB target has neither an endpoint nor a local directory")
    with LocalFalkorServer(target.local_dir, port=target.local_port, persist=target.persist) as srv:
        yield FalkorEndpoint(host=srv.host, port=srv.port)

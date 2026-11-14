"""Network/identity controls. Run only through the configured isolation launcher."""

from __future__ import annotations

import grp
import json
import os
import shutil
import socket
import threading
from pathlib import Path


SERVICE_BINARIES = (
    "docker", "dockerd", "containerd", "podman", "redis-server", "redis-cli",
    "postgres", "pg_ctl", "pg_ctlcluster", "mysqld", "mariadbd", "mongod",
    "rabbitmq-server", "memcached", "minio", "nginx", "apache2", "httpd",
)


def check_loopback() -> dict[str, object]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.settimeout(3)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted: list[bool] = []

        def accept_one() -> None:
            try:
                conn, _ = server.accept()
                conn.close()
                accepted.append(True)
            except OSError:
                accepted.append(False)

        thread = threading.Thread(target=accept_one, daemon=True)
        thread.start()
        client = socket.create_connection(("127.0.0.1", port), timeout=3)
        client.close()
        thread.join(timeout=3)
        return {"ok": bool(accepted and accepted[0]), "detail": "loopback TCP round trip"}
    except OSError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        server.close()


def check_external_blocked() -> dict[str, object]:
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(3)
    try:
        client.connect(("1.1.1.1", 53))
        return {"ok": False, "detail": "unexpected TCP connection to 1.1.1.1:53 succeeded"}
    except OSError as exc:
        return {"ok": True, "detail": f"blocked: {type(exc).__name__}: {exc}"}
    finally:
        client.close()


def check_docker_socket_inaccessible() -> dict[str, object]:
    candidates = [Path("/var/run/docker.sock"), Path("/run/docker.sock")]
    home = os.environ.get("HOME")
    if home:
        candidates.append(Path(home) / ".docker" / "run" / "docker.sock")
    attempted: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        attempted.append(str(path))
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        try:
            client.connect(str(path))
            client.close()
            return {"ok": False, "detail": f"Docker socket reachable: {path}"}
        except OSError:
            client.close()
    return {
        "ok": True,
        "detail": "Docker socket absent or unreachable",
        "paths_checked": [str(path) for path in candidates],
        "present_paths": attempted,
    }


def identity() -> dict[str, object]:
    gids = sorted(set(os.getgroups()))
    try:
        docker_gid = grp.getgrnam("docker").gr_gid
    except KeyError:
        docker_gid = None
    euid = os.geteuid()
    return {
        "euid": euid,
        "egid": os.getegid(),
        "supplementary_gids": gids,
        "non_root": euid != 0,
        "docker_gid": docker_gid,
        "no_docker_group": docker_gid is None or (
            docker_gid not in gids and docker_gid != os.getegid()
        ),
    }


def main() -> int:
    host_binaries = {name: shutil.which(name) for name in SERVICE_BINARIES}
    host_binaries = {name: path for name, path in host_binaries.items() if path}
    result = {
        "effective_identity": identity(),
        "loopback": check_loopback(),
        "external_network": check_external_blocked(),
        "docker_socket": check_docker_socket_inaccessible(),
        "host_service_binaries": host_binaries,
    }
    checks = [
        result["effective_identity"]["non_root"],
        result["effective_identity"]["no_docker_group"],
        result["loopback"]["ok"],
        result["external_network"]["ok"],
        result["docker_socket"]["ok"],
    ]
    result["passed"] = all(checks)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

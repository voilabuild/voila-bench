#!/usr/bin/env python3
"""Time to a ready service on an empty runner: postgres, redis, a Next.js app.

Docker pulls from Docker Hub and runs; voila runs the same image from
registry.voila.build. The clock runs from the start command until the service
answers a real request from the host (postgres: protocol handshake; redis: PING;
Next.js: HTTP 200 on a server-rendered page). Cold: nothing local. Warm: the
image (Docker) or the store (voila) is kept from the previous run. Stdlib only.

Cold Docker runs delete every local Docker image, so they only run where CI=true
(a throwaway runner). Elsewhere pass --skip-docker.

    VOILA_REGISTRY_TOKEN=... python3 -I services.py --reps 3 --out services-results
"""

import argparse
import json
import os
import platform
import shutil
import socket
import statistics
import struct
import subprocess
import tempfile
import time
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python3 -I leaves the script's directory off sys.path
from ci import mb, require_ci, rx_bytes, secs, version  # noqa: E402
REGISTRY = os.environ.get("VOILA_REGISTRY", "https://registry.voila.build/registry")
NEXT_APP = HERE / "workloads" / "nextjs" / ".next" / "standalone"


def pg_ready(port: int) -> bool:
    # SSLRequest: a running server answers 'S' or 'N'. During initdb the image's
    # entrypoint runs a temporary server on the unix socket only, so this means the real one.
    with socket.create_connection(("127.0.0.1", port), 1) as s:
        s.settimeout(1)
        s.sendall(struct.pack("!ii", 8, 80877103))
        return s.recv(1) in (b"S", b"N")


def redis_ready(port: int) -> bool:
    with socket.create_connection(("127.0.0.1", port), 1) as s:
        s.settimeout(1)
        s.sendall(b"PING\r\n")
        return s.recv(16).startswith(b"+PONG")


def http_ready(port: int) -> bool:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
        return r.status == 200 and b"voila bench" in r.read()


# (name, Docker Hub ref, registry.voila.build ref, container port, env, bind mount, workdir, cmd, ready)
SERVICES = [
    ("postgres 16", "postgres:16", "saucisson/bench-postgres:16", 5432, {"POSTGRES_PASSWORD": "bench"}, None, None, [], pg_ready),
    ("redis 7", "redis:7", "saucisson/bench-redis:7", 6379, {}, None, None, [], redis_ready),
    (
        "Next.js 15 app",
        "node:22",
        "saucisson/bench-node:22",
        3000,
        {"PORT": "3000", "HOSTNAME": "0.0.0.0"},
        NEXT_APP,
        "/app",
        ["node", "server.js"],
        http_ready,
    ),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(ready, port: int, started: float, logs) -> float:
    deadline = started + 300
    while time.perf_counter() < deadline:
        try:
            if ready(port):
                return time.perf_counter() - started
        except OSError:
            pass
        time.sleep(0.02)
    raise SystemExit(f"not ready after 300 s\n{logs()}")


def run_flags(env: dict[str, str], mount: Path | None, workdir: str | None) -> list[str]:
    out = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    if mount:
        out += ["-v", f"{mount}:/app"]
    if workdir:
        out += ["-w", workdir]
    return out


def docker_once(hub: str, cport: int, env, mount, workdir, cmd, ready, cold: bool) -> dict[str, float]:
    if cold:
        require_ci()
        subprocess.run(["docker", "rmi", "-f", hub], capture_output=True)
        subprocess.run(["docker", "image", "prune", "-af"], capture_output=True)
    port, name = free_port(), f"svc-{time.monotonic_ns()}"
    rx = rx_bytes()
    t = time.perf_counter()
    if cold:
        subprocess.run(["docker", "pull", "-q", hub], check=True, capture_output=True)
    pull = time.perf_counter() - t
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"{port}:{cport}", *run_flags(env, mount, workdir), hub, *cmd],
        check=True,
        capture_output=True,
    )
    logs = lambda: subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr[-2000:]  # noqa: E731
    try:
        total = wait_ready(ready, port, t, logs)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return {"total": total, "pull": pull, "rx": rx_bytes() - rx}


def voila_once(root: Path, ours: str, cport: int, env, mount, workdir, cmd, ready, flags: tuple[str, ...], trace: Path | None) -> dict[str, float]:
    venv = {
        "PATH": os.environ["PATH"],
        "HOME": str(root / "home"),
        "VOILA_ROOT": str(root / "root"),
        "VOILA_REGISTRY": REGISTRY,
        "VOILA_REGISTRY_TOKEN": os.environ["VOILA_REGISTRY_TOKEN"],
    }
    if trace:
        venv["VOILA_TRACE"] = str(trace)
    (root / "home").mkdir(parents=True, exist_ok=True)
    port, name = free_port(), f"svc-{time.monotonic_ns()}"
    rx = rx_bytes()
    t = time.perf_counter()
    subprocess.run(
        ["voila", "run", "-d", *flags, "--name", name, "-p", f"{port}:{cport}", *run_flags(env, mount, workdir), ours, *(["--", *cmd] if cmd else [])],
        env=venv,
        check=True,
        capture_output=True,
    )
    logs = lambda: subprocess.run(["voila", "logs", name], env=venv, capture_output=True, text=True).stdout[-2000:]  # noqa: E731
    try:
        total = wait_ready(ready, port, t, logs)
    finally:
        subprocess.run(["voila", "rm", name], env=venv, capture_output=True)
    return {"total": total, "rx": rx_bytes() - rx}


VARIANTS = [("voila", ()), ("voila --prefetch-dirs", ("--prefetch-dirs",))]


def markdown(res: dict[str, object]) -> str:
    env = res["env"]
    assert isinstance(env, dict)
    names = [n for n in ["Docker"] + [v for v, _ in VARIANTS] if any(n in r for r in res["services"])]  # type: ignore[union-attr]
    med = lambda runs, k: statistics.median(x[k] for x in runs)  # noqa: E731
    out = [
        "## Time until a service answers, on an empty runner",
        "",
        "From the start command until the service answers a real request from the host. Cold: nothing local",
        "(Docker pulls from Docker Hub, voila fetches from registry.voila.build). Warm: Docker image or voila store",
        "kept from the cold run. Interleaved on the same runner.",
        "",
        "| Service | " + " | ".join(f"{n} cold" for n in names) + " | " + " | ".join(f"{n} warm" for n in names) + " | Downloaded cold: " + " / ".join(names) + " |",
        "|---|" + "---|" * (2 * len(names) + 1),
    ]
    for r in res["services"]:  # type: ignore[union-attr]
        cold = [secs(med(r[n]["cold"], "total")) for n in names]
        warm = [secs(med(r[n]["warm"], "total")) for n in names]
        rx = " / ".join(mb(med(r[n]["cold"], "rx")) for n in names)
        out.append(f"| {r['service']} (`{r['image']}`) | " + " | ".join(cold) + " | " + " | ".join(warm) + f" | {rx} |")
    out += ["", "Every cold run, in order:", ""]
    for r in res["services"]:  # type: ignore[union-attr]
        out.append(f"- {r['service']}: " + "; ".join(f"{n} " + ", ".join(secs(x["total"]) for x in r[n]["cold"]) for n in names))
    out += [
        "",
        f"Medians of {env['reps']} cold and {env['reps']} warm runs each. {env['voila']}, {env['docker']}, "
        f"{env['cpus']} CPUs, kernel {env['kernel']}, {env['date']}.",
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default="services-results")
    ap.add_argument("--trace-dir", help="trace every cold voila run")
    ap.add_argument("--prefetch-dirs", action="store_true", help="also run voila with --prefetch-dirs")
    ap.add_argument("--skip-docker", action="store_true", help="voila only (for trying this outside CI)")
    a = ap.parse_args()
    if not (NEXT_APP / "server.js").exists():
        raise SystemExit(f"build the Next.js app first: (cd {NEXT_APP.parents[1]} && npm ci && npx next build)")
    if not a.skip_docker:
        require_ci()
    if a.trace_dir:
        Path(a.trace_dir).mkdir(parents=True, exist_ok=True)
    variants = VARIANTS if a.prefetch_dirs else VARIANTS[:1]
    scratch = Path(tempfile.mkdtemp(prefix="svc-"))
    results = []
    for name, hub, ours, cport, env, mount, workdir, cmd, ready in SERVICES:
        r: dict[str, object] = {"service": name, "image": hub}
        if not a.skip_docker:
            r["Docker"] = {"cold": [], "warm": []}
        for n, _ in variants:
            r[n] = {"cold": [], "warm": []}
        for rep in range(a.reps):
            if not a.skip_docker:
                r["Docker"]["cold"].append(docker_once(hub, cport, env, mount, workdir, cmd, ready, True))  # type: ignore[index]
                r["Docker"]["warm"].append(docker_once(hub, cport, env, mount, workdir, cmd, ready, False))  # type: ignore[index]
            # Rotate which voila variant goes first, so none always gets a CDN edge the
            # other just warmed.
            order = variants[rep % len(variants):] + variants[: rep % len(variants)]
            for n, flags in order:
                tag = n.replace("voila", "").strip(" -") or "plain"
                root = scratch / f"{tag}-{rep}-{time.monotonic_ns()}"
                trace = Path(a.trace_dir) / f"svc-{name.split()[0].lower().rstrip('.js')}-{tag}-{rep + 1}.json" if a.trace_dir else None
                r[n]["cold"].append(voila_once(root, ours, cport, env, mount, workdir, cmd, ready, flags, trace))  # type: ignore[index]
                r[n]["warm"].append(voila_once(root, ours, cport, env, mount, workdir, cmd, ready, flags, None))  # type: ignore[index]
                shutil.rmtree(root, ignore_errors=True)
        results.append(r)
        print(f"done: {name}", flush=True)
    res = {
        "env": {
            "voila": version(["voila", "--version"]),
            "docker": "Docker " + version(["docker", "version", "-f", "{{.Server.Version}}"]),
            "cpus": os.cpu_count(),
            "kernel": platform.release(),
            "reps": a.reps,
            "date": time.strftime("%Y-%m-%d"),
        },
        "services": results,
    }
    Path(f"{a.out}.json").write_text(json.dumps(res, indent=2))
    md = markdown(res)
    Path(f"{a.out}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""voila (registry.voila.build) vs Docker (Docker Hub): the same images, over the network.

Stdlib only. Needs `voila` and `docker` on PATH, and VOILA_REGISTRY_TOKEN for the
hub (images there are private to their org by default).

    python3 -I bench.py --reps 3 --out results
"""

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

# (label, Docker Hub ref, registry.voila.build ref, command)
COLD = [
    ("python:3.12-slim", "python:3.12-slim", "saucisson/bench-python:3.12-slim", ["python", "-c", "print(1)"]),
    ("python:3.12 (full)", "python:3.12", "saucisson/bench-python:3.12", ["python", "-c", "print(1)"]),
]
WARM = [
    ("alpine `true`", "alpine:3.20", "saucisson/bench-alpine:3.20", ["true"]),
    ("python `print(1)`", "python:3.12-slim", "saucisson/bench-python:3.12-slim", ["python", "-c", "print(1)"]),
]
REGISTRY = os.environ.get("VOILA_REGISTRY", "https://registry.voila.build/registry")


def rx_bytes() -> int:
    """Bytes received on real interfaces (not lo, docker0 or veths)."""
    total = 0
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, data = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "veth", "br-")):
            continue
        total += int(data.split()[0])
    return total


def run(cmd: list[str], env: dict[str, str] | None = None) -> float:
    t = time.perf_counter()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900)
    dt = time.perf_counter() - t
    if p.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
    return dt


def du(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def docker_reset(ref: str) -> None:
    subprocess.run(["docker", "rmi", "-f", ref], capture_output=True)
    subprocess.run(["docker", "image", "prune", "-af"], capture_output=True)


class Voila:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="voila-bench-"))
        (self.dir / "home").mkdir()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.dir / "home"),
            "VOILA_ROOT": str(self.dir / "root"),
            "VOILA_REGISTRY": REGISTRY,
            "VOILA_REGISTRY_TOKEN": os.environ["VOILA_REGISTRY_TOKEN"],
        }

    def run(self, ref: str, cmd: list[str], trace: Path | None = None) -> float:
        env = dict(self.env, VOILA_TRACE=str(trace)) if trace else self.env
        return run(["voila", "run", "--rm", ref, "--", *cmd], env)

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def cold(reps: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label, hub_ref, our_ref, cmd in COLD:
        d_t, d_rx, v_t, v_rx, v_disk = [], [], [], [], []
        for _ in range(reps):
            docker_reset(hub_ref)
            rx = rx_bytes()
            d_t.append(run(["docker", "pull", "-q", hub_ref]) + run(["docker", "run", "--rm", hub_ref, *cmd]))
            d_rx.append(rx_bytes() - rx)
            v = Voila()
            try:
                rx = rx_bytes()
                v_t.append(v.run(our_ref, cmd))
                v_rx.append(rx_bytes() - rx)
                v_disk.append(du(v.dir / "root"))
            finally:
                v.close()
        size = int(subprocess.run(["docker", "image", "inspect", "-f", "{{.Size}}", hub_ref],
                                  capture_output=True, text=True, check=True).stdout)
        rows.append({
            "image": label, "image_bytes": size,
            "docker_s": d_t, "docker_rx": d_rx,
            "voila_s": v_t, "voila_rx": v_rx, "voila_disk": v_disk,
        })
    return rows


def traced(out: Path) -> None:
    """One more cold voila run per image with VOILA_TRACE, for builds that support it."""
    out.mkdir(parents=True, exist_ok=True)
    for label, _, our_ref, cmd in COLD:
        v = Voila()
        try:
            v.run(our_ref, cmd, trace=out / (label.split(" ")[0].replace(":", "_") + ".json"))
        finally:
            v.close()


def cdn_counts(trace: Path) -> dict[str, int]:
    """cf-cache-status counts for the chunk GETs of one traced run."""
    text = trace.read_text().rstrip().rstrip(",") + "]"
    counts: dict[str, int] = {}
    for e in json.loads(text):
        args = e.get("args") or {}
        if e.get("name") == "http GET" and "/v1/" not in args.get("url", ""):
            k = args.get("cdn_cache") or "?"
            counts[k] = counts.get(k, 0) + 1
    return counts


def compare(base_bin: str, reps: int, out: Path) -> list[dict[str, object]]:
    """Alternates two voila builds in one job: same runner, same CDN edge, same hour.
    A first untimed run per image shows whether the edge had the chunks cached."""
    out.mkdir(parents=True, exist_ok=True)
    bins = {"base": base_bin, "new": shutil.which("voila") or "voila"}
    rows: list[dict[str, object]] = []
    for label, _, our_ref, cmd in COLD:
        slug = label.split(" ")[0].replace(":", "_")
        runs: list[dict[str, object]] = []
        order = ["new"] + [b for _ in range(reps) for b in ("base", "new")]
        for i, which in enumerate(order):
            v = Voila()
            trace = out / f"{slug}-{i:02d}-{which}.json"
            try:
                env = dict(v.env, VOILA_TRACE=str(trace))
                t = run([bins[which], "run", "--rm", our_ref, "--", *cmd], env)
            finally:
                v.close()
            runs.append({"build": which, "prime": i == 0, "s": t, "cdn": cdn_counts(trace)})
        rows.append({"image": label, "runs": runs})
    return rows


def compare_markdown(rows: list[dict[str, object]], env: dict[str, object]) -> str:
    out = ["## A/B in one job (cold runs, alternating)", ""]
    for r in rows:
        runs = r["runs"]  # type: ignore[index]
        prime = runs[0]
        out.append(f"### {r['image']}")
        out.append("")
        out.append(f"First (untimed) run: {prime['s']:.2f} s, CDN {prime['cdn']}")
        out.append("")
        out.append("| build | median | min | max | runs | CDN statuses |")
        out.append("|---|---|---|---|---|---|")
        for b in ("base", "new"):
            xs = [x for x in runs[1:] if x["build"] == b]
            ts = [x["s"] for x in xs]
            cdn: dict[str, int] = {}
            for x in xs:
                for k, n in x["cdn"].items():
                    cdn[k] = cdn.get(k, 0) + n
            out.append(f"| {b} | {statistics.median(ts):.2f} s | {min(ts):.2f} s | {max(ts):.2f} s | {len(ts)} | {cdn} |")
        out.append("")
        out.append("Per run: " + ", ".join(f"{x['build']} {x['s']:.2f}" for x in runs[1:]))
        out.append("")
    out.append(f"{env['cpus']} CPUs, kernel {env['kernel']}, {env['date']}.")
    return "\n".join(out) + "\n"


def warm(reps: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    v = Voila()
    try:
        for label, hub_ref, our_ref, cmd in WARM:
            run(["docker", "pull", "-q", hub_ref])
            run(["docker", "run", "--rm", hub_ref, *cmd])
            v.run(our_ref, cmd)
            d = [run(["docker", "run", "--rm", hub_ref, *cmd]) for _ in range(reps)]
            w = [v.run(our_ref, cmd) for _ in range(reps)]
            rows.append({"what": label, "docker_s": d, "voila_s": w})
    finally:
        v.close()
    return rows


def mb(n: float) -> str:
    return f"{n / 1e6:,.0f} MB" if n >= 10e6 else f"{n / 1e6:.1f} MB"


def secs(xs: list[float]) -> str:
    m = statistics.median(xs)
    return f"{m * 1000:.0f} ms" if m < 1 else f"{m:.1f} s"


def version(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def markdown(res: dict[str, object]) -> str:
    env = res["env"]
    assert isinstance(env, dict)
    out = [
        "## Cold start: pull + first run, empty machine",
        "",
        "Docker pulls from Docker Hub; voila runs straight from registry.voila.build and fetches only the file data the command reads. Network bytes are measured on the runner's interface.",
        "",
        "| Image (size unpacked) | Docker | voila | Downloaded: Docker | Downloaded: voila |",
        "|---|---|---|---|---|",
    ]
    for r in res["cold"]:  # type: ignore[union-attr]
        out.append(
            f"| {r['image']} ({mb(r['image_bytes'])}) | {secs(r['docker_s'])} | {secs(r['voila_s'])} "
            f"| {mb(statistics.median(r['docker_rx']))} | {mb(statistics.median(r['voila_rx']))} |"
        )
    out += [
        "",
        "## Warm run: image already local",
        "",
        "| Command | Docker | voila |",
        "|---|---|---|",
    ]
    for r in res["warm"]:  # type: ignore[union-attr]
        out.append(f"| {r['what']} | {secs(r['docker_s'])} | {secs(r['voila_s'])} |")
    out += [
        "",
        f"Medians: cold of {env['cold_reps']} runs, warm of {env['warm_reps']} runs. "
        f"{env['voila']}, {env['docker']}, {env['cpus']} CPUs, kernel {env['kernel']}.",
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3, help="cold-start repetitions")
    ap.add_argument("--warm-reps", type=int, default=10)
    ap.add_argument("--out", default="results")
    ap.add_argument("--quick", action="store_true", help="skip the large image")
    ap.add_argument("--compare", metavar="BASE_BIN", help="alternate this voila build with the one on PATH")
    ap.add_argument("--trace-dir", help="also do one traced cold run per image (VOILA_TRACE)")
    a = ap.parse_args()
    if a.quick:
        COLD[:] = [c for c in COLD if "full" not in c[0]]
    res: dict[str, object] = {
        "env": {
            "voila": version(["voila", "--version"]),
            "docker": "Docker " + version(["docker", "version", "-f", "{{.Server.Version}}"]),
            "cpus": os.cpu_count(),
            "kernel": platform.release(),
            "cold_reps": a.reps,
            "warm_reps": a.warm_reps,
            "date": time.strftime("%Y-%m-%d"),
        },
    }
    if a.compare:
        rows = compare(a.compare, a.reps, Path(a.trace_dir or "traces"))
        res["compare"] = rows
        Path(f"{a.out}.json").write_text(json.dumps(res, indent=2))
        md = compare_markdown(rows, res["env"])  # type: ignore[arg-type]
        Path(f"{a.out}.md").write_text(md)
        print(md)
        return
    res["cold"] = cold(a.reps)
    res["warm"] = warm(a.warm_reps)
    if a.trace_dir:
        traced(Path(a.trace_dir))
    Path(f"{a.out}.json").write_text(json.dumps(res, indent=2))
    md = markdown(res)
    Path(f"{a.out}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()

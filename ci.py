#!/usr/bin/env python3
"""A CI job on an empty runner: get the image, then build and test a project in it.

Docker pulls from Docker Hub and runs; voila runs the same image straight from
registry.voila.build. Both bind-mount a fresh copy of the project. Stdlib only.

    VOILA_REGISTRY_TOKEN=... python3 -I ci.py --reps 3 --out ci-results
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

HERE = Path(__file__).resolve().parent
REGISTRY = os.environ.get("VOILA_REGISTRY", "https://registry.voila.build/registry")

# (job, Docker Hub ref, registry.voila.build ref, project dir, env, shell command)
JOBS = [
    (
        "Go: vet, build, test",
        "golang:1.23",
        "saucisson/bench-golang:1.23",
        "go",
        {"GOFLAGS": "-buildvcs=false", "GOTOOLCHAIN": "local"},
        "go vet ./... && go build ./... && go test ./...",
    ),
    (
        "Python: pip install pytest, pytest",
        "python:3.12",
        "saucisson/bench-python:3.12",
        "python",
        {"PYTHONDONTWRITEBYTECODE": "1"},
        "pip install -q --disable-pip-version-check --root-user-action=ignore pytest"
        " && python -m pytest -q -p no:cacheprovider",
    ),
    (
        "Node: npm test",
        "node:22",
        "saucisson/bench-node:22",
        "node",
        {},
        "npm test",
    ),
]


def rx_bytes() -> int:
    total = 0
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, data = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "veth", "br-")):
            continue
        total += int(data.split()[0])
    return total


def timed(cmd: list[str], env: dict[str, str] | None = None) -> float:
    t = time.perf_counter()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
    dt = time.perf_counter() - t
    if p.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} exited {p.returncode}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
    return dt


def project_copy(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"ci-{name}-"))
    shutil.copytree(HERE / "workloads" / name, d / "src")
    return d


def run_docker(hub_ref: str, project: str, env: dict[str, str], sh: str) -> dict[str, float]:
    subprocess.run(["docker", "rmi", "-f", hub_ref], capture_output=True)
    subprocess.run(["docker", "image", "prune", "-af"], capture_output=True)
    d = project_copy(project)
    try:
        rx = rx_bytes()
        pull = timed(["docker", "pull", "-q", hub_ref])
        e = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
        job = timed(["docker", "run", "--rm", "-v", f"{d / 'src'}:/src", "-w", "/src", *e, hub_ref, "sh", "-c", sh])
        return {"total": pull + job, "pull": pull, "job": job, "rx": rx_bytes() - rx}
    finally:
        # Root-owned files the container wrote are left for the runner to discard.
        shutil.rmtree(d, ignore_errors=True)


def run_voila(
    our_ref: str, project: str, env: dict[str, str], sh: str, trace: Path | None, flags: tuple[str, ...] = ()
) -> dict[str, float]:
    d = project_copy(project)
    (d / "home").mkdir()
    venv = {
        "PATH": os.environ["PATH"],
        "HOME": str(d / "home"),
        "VOILA_ROOT": str(d / "root"),
        "VOILA_REGISTRY": REGISTRY,
        "VOILA_REGISTRY_TOKEN": os.environ["VOILA_REGISTRY_TOKEN"],
    }
    if trace:
        venv["VOILA_TRACE"] = str(trace)
    try:
        rx = rx_bytes()
        e = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
        job = timed(["voila", "run", "--rm", *flags, "-v", f"{d / 'src'}:/src", "-w", "/src", *e, our_ref, "--", "sh", "-c", sh], venv)
        return {"total": job, "job": job, "rx": rx_bytes() - rx}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def secs(x: float) -> str:
    return f"{x:.1f} s"


def mb(n: float) -> str:
    return f"{n / 1e6:,.0f} MB"


VARIANTS = [("voila", ()), ("voila --prefetch-dirs", ("--prefetch-dirs",))]


def markdown(res: dict[str, object]) -> str:
    env = res["env"]
    assert isinstance(env, dict)
    names = [n for n, _ in VARIANTS if any(n in r for r in res["jobs"])]  # type: ignore[union-attr]
    med = lambda runs, k: statistics.median(x[k] for x in runs)  # noqa: E731
    out = [
        "## CI job on an empty runner: get the image, build and test",
        "",
        "Docker: `docker pull` from Docker Hub, then `docker run`. voila: `voila run` from registry.voila.build,",
        "which fetches only the files the job reads. Same runner, interleaved, image cache wiped before each run.",
        "",
        "| Job | Docker | " + " | ".join(names) + " | Downloaded: Docker | " + " | ".join(f"Downloaded: {n}" for n in names) + " |",
        "|---|---|" + "---|" * len(names) + "---|" + "---|" * len(names),
    ]
    for r in res["jobs"]:  # type: ignore[union-attr]
        d = r["docker"]
        out.append(
            f"| {r['job']} (`{r['image']}`) | {secs(med(d, 'total'))} (pull {secs(med(d, 'pull'))}) | "
            + " | ".join(secs(med(r[n], "total")) for n in names)
            + f" | {mb(med(d, 'rx'))} | "
            + " | ".join(mb(med(r[n], "rx")) for n in names)
            + " |"
        )
    out += ["", "Every run, in order:", ""]
    for r in res["jobs"]:  # type: ignore[union-attr]
        parts = [f"Docker " + ", ".join(secs(x["total"]) for x in r["docker"])]
        parts += [f"{n} " + ", ".join(secs(x["total"]) for x in r[n]) for n in names]
        out.append(f"- {r['job']}: " + "; ".join(parts))
    out += [
        "",
        f"Medians of {env['reps']} runs each. {env['voila']}, {env['docker']}, {env['cpus']} CPUs, "
        f"kernel {env['kernel']}, {env['date']}.",
    ]
    return "\n".join(out) + "\n"


def version(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default="ci-results")
    ap.add_argument("--trace-dir", help="trace every voila run (one file per run)")
    ap.add_argument("--prefetch-dirs", action="store_true", help="also run voila with --prefetch-dirs")
    a = ap.parse_args()
    res: dict[str, object] = {
        "env": {
            "voila": version(["voila", "--version"]),
            "docker": "Docker " + version(["docker", "version", "-f", "{{.Server.Version}}"]),
            "cpus": os.cpu_count(),
            "kernel": platform.release(),
            "reps": a.reps,
            "date": time.strftime("%Y-%m-%d"),
        }
    }
    variants = VARIANTS if a.prefetch_dirs else VARIANTS[:1]
    if a.trace_dir:
        Path(a.trace_dir).mkdir(parents=True, exist_ok=True)
    jobs = []
    for job, hub_ref, our_ref, project, env, sh in JOBS:
        r: dict[str, object] = {"job": job, "image": hub_ref, "docker": []}
        for n, _ in variants:
            r[n] = []
        for rep in range(a.reps):
            r["docker"].append(run_docker(hub_ref, project, env, sh))  # type: ignore[union-attr]
            # Rotate which voila variant goes first, so none always gets a CDN edge the
            # other just warmed.
            order = variants[rep % len(variants):] + variants[: rep % len(variants)]
            for n, flags in order:
                tag = n.replace("voila", "").strip(" -") or "plain"
                trace = Path(a.trace_dir) / f"ci-{project}-{tag}-{rep + 1}.json" if a.trace_dir else None
                r[n].append(run_voila(our_ref, project, env, sh, trace, flags))  # type: ignore[union-attr]
        jobs.append(r)
    res["jobs"] = jobs
    Path(f"{a.out}.json").write_text(json.dumps(res, indent=2))
    md = markdown(res)
    Path(f"{a.out}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()

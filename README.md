# voila-bench

Benchmarks [voila](https://github.com/voilabuild/voila) against Docker on a
stock GitHub `ubuntu-24.04` runner. The latest numbers are in the
[Actions runs](https://github.com/voilabuild/voila-bench/actions/workflows/bench.yml)
(job summary, plus a `results` artifact with the raw JSON).

What it measures:

- **Cold start.** On an empty machine, the time to get `python -c 'print(1)'`
  running and the bytes downloaded to do it:
  - Docker: `docker pull` from Docker Hub, then `docker run`.
  - voila: `voila run` straight from registry.voila.build, which fetches only
    the file data the command reads.
- **Warm run.** The image is already local and the run repeats, for
  `alpine true` and `python print(1)`.

Both pull the same images. The hub copies (`saucisson/bench-*`) were
re-pushed from the Docker Hub ones with `voila import` + `voila push`.

Caveats:

- A runner is one shared VM, and the first cold run on it is noticeably
  slower for both tools. Look at the raw runs in `results.json`, not just
  the medians.
- Docker runs with its daemon already started; voila has no daemon.
- Docker Hub is a mature CDN; registry.voila.build is a single small
  deployment.
- voila is alpha software.

Run it yourself (Linux, with voila and Docker installed):

```bash
VOILA_REGISTRY_TOKEN=... python3 -I bench.py --reps 3
```

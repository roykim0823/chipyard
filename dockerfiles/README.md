# Chipyard Install Notes (this devcontainer)

How Chipyard is set up in this devcontainer, including the extra installs and
repo edits that the stock `build-setup.sh` does **not** handle on a minimal
Ubuntu 24.04 host (glibc 2.39). The fixes in §3 are now baked into the
devcontainer image (`.devcontainer/Dockerfile` / `dockerfiles/Dockerfile`); the
Chipyard repo itself is **bind-mounted from the host**, not cloned into the image.

- Chipyard: `github.com/ucb-bar/chipyard` @ `48f904ae` (bind-mounted from host)
- Host: Ubuntu 24.04, **glibc 2.39**, x86_64
- Built env lives in `/home/vscode/chipyard/.conda-env` (~7.4 GB, on the host)

---

## 1. Quick Start

The prerequisites (§3) are already in the image and the glibc 2.39 lockfile is
already pinned in this checkout, so the build itself is just `./build-setup.sh
riscv-tools`. Pick the path that matches your situation.

### First run (fresh image)

On a freshly built image the `conda activate base` line commonly hits two issues
(the conda shell hook isn't loaded, and a zstd/zstandard version mismatch breaks
the solver) — fix both up front, then build:

```bash
cd ~/chipyard                                                             # bind-mounted host repo (/home/vscode/chipyard)
source /opt/conda/etc/profile.d/conda.sh                                  # CondaError: Run 'conda init' before 'conda activate'
python -m pip install --user --force-reinstall --no-cache-dir zstandard   # zstd C API versions mismatch (§4.1)
conda activate base
./build-setup.sh riscv-tools
```

### Re-run (rebuild over an existing checkout)

conda is already healthy from the first run (the `--user` zstandard fix lives in
`~/.local`), so the only extra step is clearing the env dirs — step 1 aborts if
they already exist:

```bash
cd ~/chipyard
source /opt/conda/etc/profile.d/conda.sh   # new shell: CondaError: Run 'conda init' before 'conda activate'
conda activate base
rm -rf .conda-lock-env .conda-env          # step 1 aborts if these exist
./build-setup.sh riscv-tools
```

- Run **only one** `build-setup.sh` at a time. Two concurrent runs both `rm -rf
  .conda-env` and collide (symptom: spurious `.conda-env/bin/install: not found`,
  `Error 127`).
- The 11 steps: 1 conda env · 2 submodules · 3 toolchain · 4 ctags · 5 Scala
  precompile · 6 FireSim · 7 FireSim precompile · 8 FireMarshal · 9 buildroot
  precompile · 10 CIRCT · 11 cleanup.

### Resuming after a fix
Skip completed steps with repeated `-s N`, and re-source the env first:
```bash
source env.sh                                        # conda is already on PATH
./build-setup.sh -s 1 -s 2 -s 3 -s 4 riscv-tools     # e.g. resume at step 5
```

---

## 2. Daily use (after install)

```bash
cd ~/chipyard        # bind-mounted host repo (/home/vscode/chipyard)
source env.sh        # activates .conda-env; needs conda on PATH first
```
`env.sh` requires `conda` to be resolvable; in the devcontainer it's already on
`PATH` (system `/opt/conda`, set up by the image). Outside the devcontainer,
`source ~/miniforge3/etc/profile.d/conda.sh` first (or add Miniforge to `~/.bashrc`).

---

## 3. Upstream fixes (baked in)

Deviations from stock Chipyard that the image and this checkout handle for you —
no manual step needed inside the devcontainer. Documented here for the record and
for anyone building Chipyard outside this devcontainer.

### a) Prerequisites — conda & kmod

Missing from a stock Ubuntu 24.04 base, so the Dockerfile installs both up front.

**conda** (required by step 1): the Dockerfile fetches Chipyard's own
`install-conda.sh` (standalone, at the pinned commit) and installs conda
**system-wide to `/opt/conda`** — with `conda-build`, the libmamba solver, the
`ucb-bar` channel, and `conda-lock` — and puts `/opt/conda/bin` on `PATH`. Without
conda, step 1 fails with `conda: command not found` (exit 127). Outside the
devcontainer, install Miniforge manually instead:
```bash
cd /tmp
curl -fsSLO "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
bash Miniforge3-Linux-x86_64.sh -b -p "$HOME/miniforge3"
```

**kmod (`depmod`)** (required by step 9, FireMarshal): the Dockerfile installs
`kmod` via apt. Without it, step 9 fails with `FileNotFoundError: 'depmod'`.
Outside the devcontainer:
```bash
sudo apt-get update && sudo apt-get install -y kmod
```

> ✅ Both are part of the image (`.devcontainer/Dockerfile` /
> `dockerfiles/Dockerfile`), so they survive a devcontainer rebuild. Only the
> `.conda-env` build output lives in the bind-mounted repo on the host.

### b) glibc 2.39 lockfile (one-time, already done in this checkout)

The repo ships conda lockfiles pinned to `sysroot_linux-64=2.34`. On this glibc
**2.39** host that mismatch causes:
- step 1: `build-setup.sh` auto-runs `generate-conda-lockfiles.sh`, which deletes
  the lockfile before regenerating — fragile if interrupted; and
- step 3: `riscv-isa-sim` fails to link with `undefined reference to __isoc23_strtol`
  (a glibc ≥ 2.38 symbol) because the conda toolchain's glibc (2.34) is older than
  the host headers (2.39).

**Resolution (kept in this checkout):** the lockfiles were regenerated for glibc
2.39. If starting from a *fresh* clone on a 2.39 host, reproduce with:
```bash
# chipyard-base.yaml: set  sysroot_linux-64=2.39  (build-setup does this sed automatically)
conda activate base    # conda is on PATH (system /opt/conda) in the devcontainer
./scripts/generate-conda-lockfiles.sh     # needs conda-lock 2.5.7; run inside .conda-env or .conda-lock-env
```
This pins `sysroot_linux-64=2.39` (available on conda-forge) in:
- `conda-reqs/conda-lock-reqs/conda-requirements-riscv-tools-linux-64.conda-lock.yml`
- `conda-reqs/conda-lock-reqs/conda-requirements-riscv-tools-linux-64-lean.conda-lock.yml`

---

## 4. Troubleshooting (issues actually hit, in order)

| Step | Symptom | Cause | Fix |
|------|---------|-------|-----|
| 1 | `conda: command not found` | conda not on `PATH` (only when building outside the image) | conda is baked into the image; otherwise install Miniforge (§3a) |
| 1 | `CondaError: Run 'conda init' before 'conda activate'` | conda shell hook not loaded in this terminal (image inits root's profile, not the `vscode` user's) | `source /opt/conda/etc/profile.d/conda.sh` (works immediately); to persist, `conda init bash` then reopen the terminal |
| 1 | `zstd C API versions mismatch (10507 ... 10502)`; `conda-libmamba-solver` / `conda-pypi` entry points fail to load | the `zstandard` C-extension was built against zstd 1.5.2 (10502) but `conda update conda` pulled libzstd 1.5.7 (10507); breaks the default libmamba solver | `pip install --user` the PyPI `zstandard` (bundles its own libzstd, no sudo) — see §4.1 below |
| 1 | `conda-lock install` prints help, exit 1 | lockfile path missing (deleted by glibc regen) | `git checkout -- conda-reqs/conda-lock-reqs/<file>`; see §3b |
| 3 | `undefined reference to __isoc23_strtol` | conda glibc 2.34 < host 2.39 | regenerate lockfile for sysroot 2.39 (§3b) |
| 3 | `.conda-env/bin/install: not found` (Error 127) | two builds running, one `rm -rf`'d the env | run a single build only |
| 5 | `not found: object gemmini` / `type Gemmini` | `generators/gemmini` working tree source deleted | `cd generators/gemmini && git checkout -- .` |
| 8 | seems frozen, no output | cloning `firesim/linux.git` kernel (`--filter=tree:0`); GitHub enumerates ~10 min at 0 bytes | wait; `du -sh .git/modules/software/firemarshal/modules/riscv-linux` should grow |
| 9 | `FileNotFoundError: 'depmod'` | no `kmod` package | kmod is baked into the image; otherwise `sudo apt-get install -y kmod` (§3a) |

### 4.1 conda solver / zstd mismatch repair

`conda update conda` (run by `install-conda.sh`) can leave the base env with a
newer `libzstd` (1.5.7 → version number `10507`) than the conda-shipped
`zstandard 0.19.0` binding was compiled against (zstd 1.5.2 → `10502`). The symptom
is harmless-looking warnings plus two failed entry points — but one of them is
`conda-libmamba-solver`, the default solver, so `build-setup.sh` step 1 can fail
to create the env.

**The conda-side reinstall does not work in this devcontainer.** The matched-pair
classic-solver approach (`conda install -n base --solver=classic --force-reinstall
-y zstandard zstd`) fails here because:
- `/opt/conda` is owned by uid 1000, so as the `vscode` user the command dies with
  `EnvironmentNotWritableError` unless run via `sudo`; and
- even with `sudo`, `--force-reinstall` just puts the *same* incompatible pair back;
  downgrading `zstd=1.5.2` to match fails to solve (dependency conflicts); and conda
  can't pull a newer matched `zstandard` because reading `.conda`/`.zst` packages
  itself needs a working `zstandard` (chicken-and-egg).

**Fix it with a `--user` pip install (no sudo).** The PyPI `zstandard` wheel
statically bundles its own libzstd, sidestepping conda's mismatched lib entirely.
Installing it into your user site (`~/.local`, owned by `vscode`) shadows the broken
conda copy on `sys.path` — and because `/opt/conda/bin/conda`'s python runs as the
same uid with `ENABLE_USER_SITE=True`, it fixes the libmamba solver too:

```bash
python -m pip install --user --force-reinstall --no-cache-dir zstandard
```

Verify the warnings are gone and conda is healthy:

```bash
source /opt/conda/etc/profile.d/conda.sh && conda activate base       # should activate with no warnings
python -c "import zstandard; print(zstandard.__file__, zstandard.__version__)"  # path under ~/.local, e.g. 0.25.0
conda info        # should print with no 'Error while loading conda entry point' lines
```

> The `--user` fix is tied to the `vscode` user's home and to this Python minor
> (`python3.10` user-site). To fix it image-wide for *any* user instead, install
> into the system env with sudo: `sudo /opt/conda/bin/python -m pip install
> --force-reinstall --no-cache-dir zstandard`.
>
> Neither variant survives a devcontainer rebuild (both live outside the
> bind-mounted repo). To make it permanent, add a
> `/opt/conda/bin/python -m pip install --force-reinstall --no-cache-dir zstandard`
> step after `install-conda.sh` in the Dockerfile.

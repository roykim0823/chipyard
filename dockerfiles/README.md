# Chipyard Install Notes (this devcontainer)

How Chipyard was successfully set up in this environment, including the extra
installs and repo edits that the stock `build-setup.sh` does **not** handle on a
minimal Ubuntu 24.04 devcontainer (glibc 2.39).

- Chipyard: `github.com/ucb-bar/chipyard` @ `48f904ae`
- Host: Ubuntu 24.04, **glibc 2.39**, x86_64
- Built env lives in `project/chipyard/.conda-env` (~7.4 GB)

---

## 1. Prerequisites (install before `build-setup.sh`)

These are missing from the base devcontainer and must be installed first.

### a) Miniforge (conda) — required by step 1
```bash
cd /tmp
curl -fsSLO "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
bash Miniforge3-Linux-x86_64.sh -b -p "$HOME/miniforge3"
```
Without this, step 1 fails with `conda: command not found` (exit 127).

### b) kmod (`depmod`) — required by step 9 (FireMarshal)
```bash
sudo apt-get update && sudo apt-get install -y kmod
```
Without this, step 9 fails with `FileNotFoundError: 'depmod'`.

> ⚠️ Both live outside the container image (`$HOME` / apt layer) and are **wiped
> on a devcontainer rebuild**. For a durable setup, bake them into a Dockerfile
> (see `../chipyard.Dockerfile`).

---

## 2. glibc 2.39 lockfile (one-time, already done in this checkout)

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
source ~/miniforge3/etc/profile.d/conda.sh && conda activate base
./scripts/generate-conda-lockfiles.sh     # needs conda-lock 2.5.7; run inside .conda-env or .conda-lock-env
```
This pins `sysroot_linux-64=2.39` (available on conda-forge) in:
- `conda-reqs/conda-lock-reqs/conda-requirements-riscv-tools-linux-64.conda-lock.yml`
- `conda-reqs/conda-lock-reqs/conda-requirements-riscv-tools-linux-64-lean.conda-lock.yml`

---

## 3. Run the build

```bash
cd /workspaces/polyglot_devcontainer/project/chipyard
source ~/miniforge3/etc/profile.d/conda.sh && conda activate base
rm -rf .conda-lock-env .conda-env        # only if re-running; step 1 aborts if these exist
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
source ~/miniforge3/etc/profile.d/conda.sh && source env.sh
./build-setup.sh -s 1 -s 2 -s 3 -s 4 riscv-tools     # e.g. resume at step 5
```

---

## 4. Troubleshooting (issues actually hit, in order)

| Step | Symptom | Cause | Fix |
|------|---------|-------|-----|
| 1 | `conda: command not found` | no conda in devcontainer | install Miniforge (§1a) |
| 1 | `conda-lock install` prints help, exit 1 | lockfile path missing (deleted by glibc regen) | `git checkout -- conda-reqs/conda-lock-reqs/<file>`; see §2 |
| 3 | `undefined reference to __isoc23_strtol` | conda glibc 2.34 < host 2.39 | regenerate lockfile for sysroot 2.39 (§2) |
| 3 | `.conda-env/bin/install: not found` (Error 127) | two builds running, one `rm -rf`'d the env | run a single build only |
| 5 | `not found: object gemmini` / `type Gemmini` | `generators/gemmini` working tree source deleted | `cd generators/gemmini && git checkout -- .` |
| 8 | seems frozen, no output | cloning `firesim/linux.git` kernel (`--filter=tree:0`); GitHub enumerates ~10 min at 0 bytes | wait; `du -sh .git/modules/software/firemarshal/modules/riscv-linux` should grow |
| 9 | `FileNotFoundError: 'depmod'` | no `kmod` package | `sudo apt-get install -y kmod` (§1b) |

---

## 5. Daily use (after install)

```bash
cd /workspaces/polyglot_devcontainer/project/chipyard
source env.sh        # activates .conda-env; needs conda on PATH first
```
`env.sh` requires `conda` to be resolvable, so in a fresh shell run
`source ~/miniforge3/etc/profile.d/conda.sh` first (or add Miniforge to `~/.bashrc`).

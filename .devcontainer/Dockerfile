# Chipyard devcontainer image: installs all prerequisites (apt deps + conda) as a
# non-root `vscode` user, but does NOT clone or build Chipyard. The Chipyard
# source is bind-mounted from the host at /home/vscode/chipyard (see
# devcontainer.json), and you run the build inside the container:
#
#   cd ~/chipyard
#   conda activate base
#   ./build-setup.sh riscv-tools
#
# Derived from chipyard/dockerfiles/Dockerfile, with these fixes/additions:
#   - ubuntu:24.04 (glibc 2.39) instead of 20.04; build-setup.sh auto-regenerates
#     the conda lockfiles for sysroot_linux-64=2.39 (avoids the __isoc23_strtol
#     link error from the stock 2.34-pinned lockfiles).
#   - adds kmod (depmod), required by FireMarshal; the original omits it.
#   - reuses Chipyard's own .github/scripts/install-conda.sh for conda setup.
#   - non-root `vscode` user (uid 1000) with passwordless sudo, for devcontainer use.
#   - does not clone Chipyard (bind-mounted from the host) and does not run the
#     stale `setup.sh` (renamed to build-setup.sh upstream).

FROM ubuntu:24.04

# Pin the Chipyard commit whose install-conda.sh is fetched; override with
# --build-arg CHIPYARD_HASH=<sha>.
ARG CHIPYARD_HASH=48f904ae
ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-c"]

# Host-level deps. Most of Chipyard's toolchain comes from conda; these cover
# cloning, the conda installer, FireMarshal's cached-image unzip, depmod, a few
# host-compiled helpers, plus basics expected inside a devcontainer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        git \
        sudo \
        bc \
        unzip \
        build-essential \
        kmod \
        locales \
        openssh-client \
        less \
        procps \
        vim \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Create the non-root `vscode` user. ubuntu:24.04 ships an `ubuntu` user at
# uid 1000, so remove it first to free the id, then add `vscode` with sudo.
RUN userdel -r ubuntu 2>/dev/null || true \
    && groupadd --gid ${USER_GID} ${USERNAME} \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m -s /bin/bash ${USERNAME} \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

# Install conda via Chipyard's own canonical bootstrap (system-wide /opt/conda,
# conda-build + libmamba solver + ucb-bar channel, conda init in /etc bashrc).
# The script is fetched standalone at the pinned commit -- the rest of Chipyard
# is bind-mounted from the host, not baked into the image.
# Patch: `conda update conda` pulls 23.9.0, which renamed `experimental_solver`
# to `solver` (conda 23.7+). The pinned script still uses the old key and fails
# with `CondaValueError: Key 'experimental_solver' is not a known primitive parameter.`
RUN curl -fsSL "https://raw.githubusercontent.com/roykim0823/chipyard/${CHIPYARD_HASH}/.github/scripts/install-conda.sh" -o /tmp/install-conda.sh \
    && sed -i 's/--set experimental_solver libmamba/--set solver libmamba/' /tmp/install-conda.sh \
    && bash /tmp/install-conda.sh \
    && rm /tmp/install-conda.sh

# Hand conda to the non-root user so the build (which writes conda's pkg cache)
# needs no sudo. The repo itself is bind-mounted and keeps its host ownership.
RUN chown -R ${USER_UID}:${USER_GID} /opt/conda

ENV PATH=/opt/conda/bin:${PATH}
USER ${USERNAME}
WORKDIR /home/${USERNAME}/chipyard

# Build is intentionally left to the user:
#   conda activate base && ./build-setup.sh riscv-tools
CMD ["/bin/bash"]

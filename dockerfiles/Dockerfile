# Chipyard devcontainer image: clones Chipyard + installs all prerequisites as a
# non-root `vscode` user, but does NOT build it. You run the build inside the
# container (e.g. from a devcontainer terminal):
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
#   - does not run the stale `setup.sh` (renamed to build-setup.sh upstream).

FROM ubuntu:24.04

# Pin the Chipyard commit to clone; override with --build-arg CHIPYARD_HASH=<sha>.
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

# Clone Chipyard at the pinned commit into the user's home.
RUN git clone https://github.com/ucb-bar/chipyard.git /home/${USERNAME}/chipyard \
    && cd /home/${USERNAME}/chipyard \
    && git checkout ${CHIPYARD_HASH}

# Install conda via Chipyard's own canonical bootstrap (system-wide /opt/conda,
# conda-build + libmamba solver + ucb-bar channel, conda init in /etc bashrc).
RUN /home/${USERNAME}/chipyard/.github/scripts/install-conda.sh

# Hand both the repo and conda to the non-root user so the build (which writes
# .conda-env inside the repo and conda's pkg cache) needs no sudo.
RUN chown -R ${USER_UID}:${USER_GID} /home/${USERNAME}/chipyard /opt/conda

ENV PATH=/opt/conda/bin:${PATH}
USER ${USERNAME}
WORKDIR /home/${USERNAME}/chipyard

# Build is intentionally left to the user:
#   conda activate base && ./build-setup.sh riscv-tools
CMD ["/bin/bash"]

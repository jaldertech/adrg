FROM python:3.11-slim-bookworm

WORKDIR /app

# !! SECURITY NOTICE !!
# ADRG requires privileged access to function. To write cgroup v2 control
# files and manage container resources, the container must be run with:
#   --privileged
# This grants ADRG full control over the host kernel's resource management
# subsystem. It is the same level of access required by any tool that writes
# to /sys/fs/cgroup/. This is not a flaw in ADRG — it is an inherent
# requirement of the Linux cgroup interface.
#
# Recommendation: audit this codebase before running it. ADRG is fully
# open source for this reason.
#
# Minimal run example:
#   docker run -d \
#     --name adrg \
#     --privileged \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v /sys/fs/cgroup:/sys/fs/cgroup \
#     -v /proc:/host/proc:ro \
#     -v /etc/adrg/config.yaml:/app/config.yaml:ro \
#     --env-file /etc/adrg/adrg.env \
#     ghcr.io/jaldertech/adrg

# systemd-python is intentionally omitted: it requires libsystemd-dev at
# build time and provides no benefit when running in Docker.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY adrg.py .
COPY modules/ modules/

CMD ["python3", "adrg.py", "--config", "/app/config.yaml"]

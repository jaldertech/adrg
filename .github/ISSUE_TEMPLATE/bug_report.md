---
name: Bug report
about: Create a report to help us improve
title: "[BUG] "
labels: bug
assignees: ''
---

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behaviour.

**Expected behaviour**
What you expected to happen.

**Logs**
Please paste relevant logs from `journalctl -u adrg -f` or `docker logs adrg`.

**Environment:**
 - OS: [e.g. Raspberry Pi OS Bookworm]
 - Hardware: [e.g. Raspberry Pi 5 16GB]
 - Kernel: `uname -a`
 - Python: `python3 --version`
 - Docker: [version, e.g. from `docker --version`]
 - cgroup v2: `ls /sys/fs/cgroup/cgroup.controllers` (confirm it exists)
 - Installation: [Host (systemd) or Docker]

**Config:** If relevant, paste the affected part of `config.yaml` with secrets redacted.

**Additional context**
Add any other context about the problem here.

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-03-01
### Added
- **PyPI Distribution:** Added `pyproject.toml` and standard package structure to allow installing ADRG via `pip install adrg`.
- Updated `README.md` to include PyPI installation instructions as the primary quick start method.

## [1.0.0] - 2026-02-28
### Added
- Initial public release of the Aldertech Dynamic Resource Governor (ADRG).
- Dynamic Resource Monitoring (CPU, Memory, I/O, Thermal).
- Media Mode parsing for Jellyfin/Plex playback.
- Sub-container discovery for complex stacks (e.g. qBittorrent, VPNs).
- Webhook endpoints for manual/external triggers.
- Support for systemd and Docker deployment.

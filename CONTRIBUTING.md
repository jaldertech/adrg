# Contributing to ADRG

Thanks for your interest in contributing to ADRG (Aldertech Dynamic Resource Governor). This document gives a short guide to opening issues and submitting changes.

## Before you start

- Read the [README](README.md) to understand what ADRG does and how it works.
- Check existing [issues](https://github.com/jaldertech/adrg/issues) and [pull requests](https://github.com/jaldertech/adrg/pulls) to avoid duplicates.

## Opening an issue

- **Bug reports:** Describe what you did, what you expected, and what happened. Include your environment (OS, kernel, Python version, Docker version) and relevant config (redact secrets).
- **Feature ideas:** Explain the use case and how it fits with ADRG’s goals (resource governance for home servers, cgroup v2, media-aware throttling).
- **Questions:** Open an issue and use the “Question” label if you have it; otherwise a normal issue is fine.

## Submitting changes (pull requests)

1. **Fork the repo** and create a branch from `main` (e.g. `fix/thing` or `feature/thing`).
2. **Make your changes** in that branch. Keep the scope focused.
3. **Match existing style:** Python 3.9+, type hints where it helps, same logging and error-handling style as the rest of the codebase. No new runtime dependencies without discussion.
4. **Update docs** if you change behaviour or config: README, `config.yaml` comments, and/or docstrings.
5. **Test:** Run `python3 adrg.py --check-config` with your config, and use `--dry-run` where relevant. If you can, test on a real Linux system with cgroup v2 and Docker.
6. **Open a PR** against `main` with a clear title and description of what changed and why. Reference any related issues.

## What we’re looking for

- **Bug fixes** and **documentation** improvements are always welcome.
- **New features** (e.g. extra media providers, notification backends, rules): open an issue first so we can align on design and scope.
- **Config / UX:** Keep config in YAML, env vars for secrets, and avoid breaking existing `config.yaml` layouts when possible.

## Code of conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). Be respectful and constructive.

## Licence

By contributing, you agree that your contributions will be licensed under the same [MIT Licence](LICENCE) as the project.

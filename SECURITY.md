# Security Policy

dani is an experimental project. Treat every `0.x` release as public-preview software for maintainer-operated trials.

## Supported Versions

Only the latest released `0.x` version and the current `main` branch are considered for security fixes.

## Reporting a Vulnerability

Use a GitHub Security Advisory for private vulnerability reports when available. If that is not available, contact the maintainer listed in `pyproject.toml`.

Do not put secrets, webhook payloads with tokens, private repository URLs, or exploit details in public issues or pull requests.

## Operational Notes

- Run dani with the least-privileged GitHub token that can perform the required repository actions.
- Keep `DANI_WEBHOOK_SECRET`, `DANI_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_PAT` out of logs and screenshots.
- Expose `dani serve` only through a TLS-terminating proxy or tunnel that you control.

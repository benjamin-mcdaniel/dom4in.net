# Contributing

Thanks for your interest. This is a personal/portfolio project, so contributions are welcome but the scope is intentionally narrow.

## Getting started

1. Fork the repo and create a branch from `main`.
2. Follow the setup steps in the README.
3. Make your change, test it locally.
4. Open a PR — the template will guide you.

## What's in scope

- Bug fixes in the frontend, Worker, or collector
- Performance or reliability improvements to the collector
- New aggregation dimensions (e.g. additional TLDs, charset extensions)
- UI improvements to `frontend/index.html`

## What's out of scope

- Per-domain data storage or WHOIS lookups
- Anything that requires a persistent server (the project is designed to run serverlessly)

## Code style

- JS: no framework, keep it readable, prefer explicit over clever.
- Python: standard library where possible, minimal dependencies.
- No linter config is enforced — just match the surrounding style.

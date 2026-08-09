# Security Policy

## Supported Versions

OmniFlow is currently in controlled alpha. Security fixes are applied to the latest tagged release only.

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub private vulnerability reporting for this repository. Include the affected version, reproduction steps, impact, and any suggested mitigation.

Maintainers will review complete reports on a best-effort basis. No service-level commitment is implied while the project remains in alpha.

## Security Boundaries

- OmniFlow reads `OMNI_API_KEY` only from the process environment.
- Pull-request policy and model host metadata are read from the trusted base branch.
- The example privileged workflow does not check out or execute pull-request code; changed filenames are read through GitHub's API.
- Public artifacts are redacted; detailed model and dependency artifacts remain local unless explicitly uploaded.
- OmniFlow never runs warehouse queries and must not persist raw query results.

See the README for the current live-testing limitations and least-privilege guidance.

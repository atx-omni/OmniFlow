# Testing OmniFlow

OmniFlow uses four evidence layers. A green result in one layer must not be presented as proof of another.

1. Unit and contract tests verify parsing, policy, redaction, retries, routing, report formats, and failure behavior.
2. The deterministic alpha simulator exercises complete CLI runs against a bounded fake Omni API.
3. GitHub Actions verifies the packaged action, pinned dependencies, supported Python versions, security scans, and artifacts.
4. The adopter live gate verifies the customer's Omni tenant, token permissions, Git integration, branch mapping, content, and GitHub protections.

## Capability Matrix

| Capability | Automated evidence | Required live evidence |
| --- | --- | --- |
| Omni and non-Omni PR routing | Unit tests and simulator | One dbt-only or application-only PR skips; one Omni PR runs |
| Trusted model discovery | Unit tests for single, multi-model, marker, push, fork, and ambiguity paths | Omni-created PR resolves the expected model and branch |
| Model validation | Unit parser and policy tests | Valid and intentionally invalid branch checks |
| Content validation | Unit extraction, labels, history, base comparison, and redaction tests | Tenant Content Validator completes with expected scope |
| YAML pull | Unit security and manifest tests | Authored and fully resolved pulls complete with checksums |
| Semantic diff and lint | Unit change and rule tests | A harmless semantic change produces the expected risk summary |
| Downstream contracts | Unit and simulator reference tests | Targeted content searches complete without coverage gaps |
| dbt exposures | Unit normalization, partial coverage, failure policy, and simulator tests | Base and branch calls return expected dashboard and dependency counts |
| JSON, Markdown, SARIF, and JUnit | Unit render tests and packaged action tests | Public artifact downloads open and contain only redacted evidence |
| GitHub annotations and PR summary | Unit escaping tests | A controlled PR displays warnings or errors and updates one bot comment |
| AI Repair development scaffold | Unit rollback and safety tests | Maintainer-only non-production failure, repair, rerun, and rollback exercises |
| Release supply chain | Workflow and repository hardening tests | Protected release environment produces signed artifacts, checksums, and SBOM |

## Local Regression Gate

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade "pip==26.1.2" "setuptools==83.0.0"
python -m pip install -e ".[dev]"
pytest --cov=omniflow
ruff check .
bandit -c pyproject.toml -r src
python scripts/simulate_alpha.py
python -m build
twine check dist/*
```

The simulator opens a temporary localhost port. Run it in an environment that permits loopback network access.

## Live dbt Exposure Gate

The [Omni dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures) requires Connection Admin permission and supports `branch_id`. Before enabling the check as a gate:

1. Confirm the Omni connection has a formal dbt integration.
2. Confirm at least one published shared dashboard references dbt models through Omni model references.
3. Use a dedicated Personal Access Token with only the permissions needed for the enabled checks.
4. Temporarily set `checks.dbt_exposures.fail_on_unavailable: true` for the first controlled run.
5. Run both the base model and an existing Omni branch.
6. Verify `total_records`, `total_exposures`, `unmapped_dashboards`, dependency counts, and `coverage_status` in the JSON and Markdown reports.
7. Verify no owner email, dashboard URL, token, or raw query result appears in public artifacts.

Omni documents that exposure generation covers published dashboards in shared space. Private dashboards and workbooks are not included. Direct database-table references can also prevent dbt dependency mapping; use `${model_name}` references where lineage is required. See [Pushing exposures to dbt](https://docs.omni.co/integrations/dbt/exposures).

dbt exposure enrichment supplements OmniFlow's targeted Content Validator contract analysis. It does not replace it and it does not run `dbt build`.

## Intentionally Bounded Tests

- Do not run `--unsafe-raw-output` against customer content as a routine test. Its behavior is covered with synthetic payloads.
- Do not send secrets to fork pull requests. Fork routing is proven with withheld-secret tests and should fail closed for Omni changes.
- Do not test AI rollback or destructive model changes against production models.
- Do not publish a release merely to test release automation. Use the protected release gate for intentional versions only.

Record the workflow URL, commit SHA, policy decision, artifact hash or retention ID, and any coverage gaps for each adopter live gate. A local pass does not establish tenant permissions, branch protection, or production readiness.

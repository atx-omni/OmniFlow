# OmniFlow

OmniFlow is a local-first, security-first CI/CD orchestrator for Omni semantic-layer development. It is designed for the Omni-created pull request workflow: the PR supplies non-secret model context, GitHub Actions runs `omniflow run --auto`, and reviewers get validation, downstream contract impact, governance, and audit evidence before merge.

## Customer Quickstart

1. Add the OmniFlow GitHub workflow to the connected git repository.
2. Store `OMNI_API_KEY` as a GitHub Actions secret.
3. Use the copied workflow from `.github/workflow-examples/omniflow.yml`.
4. Open a pull request from Omni.

The default action command is:

```bash
omniflow run --auto
```

For alpha testing, install OmniFlow through the pinned GitHub Action checkout:

```yaml
- uses: atx-omni/omniflow@<pinned-commit-sha>
  env:
    OMNI_API_KEY: ${{ secrets.OMNI_API_KEY }}
```

Do not use `pip install omniflow` for alpha testing unless this project has been explicitly published to PyPI under the expected version.

No customer-managed `base_url`, `model_id`, or `branch_name` is required in policy config. OmniFlow uses trusted repo metadata or explicit CI environment values for Omni host identity, and it can read the `omniflow-context` PR marker when present for model and branch selection.

## PR Context And Metadata

Optional PR marker:

```html
<!-- omniflow-context {"model_id":"uuid","model_path":"omni/my_model","branch_name":"feature/my-change"} -->
```

Trusted fallback metadata:

- `.omni/flow.json`: non-secret model/repository identity for one or more Omni models.

An example file is included at `.omni/flow.example.json`.

The PR marker must not provide `base_url`. OmniFlow only accepts Omni host identity from trusted sources such as `.omni/flow.json`, `OMNI_BASE_URL`, or explicit CLI flags. This prevents a pull request body from redirecting CI secrets to an untrusted host.

## PR Routing

OmniFlow is safe to install in repositories that also receive dbt, docs, or application-code pull requests. When `omniflow run --auto` does not find an Omni PR marker and cannot match changed files to Omni-managed metadata, it exits `0` with a skipped policy decision and still writes evidence artifacts.

Malformed Omni metadata, secret-like keys, or an Omni PR marker that cannot be resolved still fail the run. This keeps non-Omni work quiet while preserving strong gates for semantic-layer changes.

## Downstream Impact Generation

OmniFlow does not require customers to commit a dependency graph. During `omniflow run --auto`, it:

1. Pulls base and branch YAML from Omni.
2. Computes a semantic diff.
3. Calls Omni Content Validator search for changed fields, views, topics, and relationship references.
4. Generates `.omniflow/<model_id>/dependencies.json` as an evidence artifact.
5. Evaluates contract impact against that generated artifact.

If targeted dependency search is unavailable, OmniFlow falls back to full content validation impact and records that fallback mode in the artifact.

## Optional Policy Config

`.omniflow.yml` is optional and controls policy only:

```yaml
contracts:
  enabled: true
  fail_on:
    deleted_referenced_fields: true
    renamed_referenced_fields: true
    referenced_field_type_changes: true
    referenced_join_cardinality_changes: true

checks:
  content_validation:
    enabled: true
    fail_on_new_only: true

  model_validation:
    enabled: true
    fail_on_warnings: false

  dbt_exposures:
    enabled: false
    fail_on_unavailable: false
```

If any config or Omni-managed metadata key matches `api_key`, `token`, `secret`, or `password`, OmniFlow fails before running.

## CLI

```bash
omniflow doctor --auto
omniflow run --auto
omniflow content validate --base-url https://example.omniapp.co --model-id <id>
omniflow model validate --base-url https://example.omniapp.co --model-id <id>
omniflow yaml pull --base-url https://example.omniapp.co --model-id <id> --out .omniflow/yaml
omniflow exposures pull --base-url https://example.omniapp.co --model-id <id>
omniflow diff --base path/to/base/yaml --head path/to/head/yaml
```

Explicit identity flags are retained for debugging and local advanced usage only.

## Evidence Artifacts

`omniflow run --auto` writes:

- `.omniflow/report.json`
- `.omniflow/report.md`
- `.omniflow/report.sarif`
- `.omniflow/junit.xml`
- `.omniflow/evidence.json`
- `.omniflow/public/`
- `.omniflow/restricted/`
- per-model semantic diff and contract impact artifacts

Reports include IDs, names, owners, labels, paths, summaries, risk levels, config hash, git SHA, branch, PR number, model ID, and policy decision. Reports must not include API keys, raw query results, or raw Omni payloads.

The example GitHub workflow uploads only `.omniflow/public/` summary artifacts and `.omniflow/artifact-manifest.json` by default. Per-model YAML pulls, dependency graphs, exposure details, and detailed contract artifacts live under `.omniflow/restricted/` and remain local CI workspace files unless a team explicitly chooses to upload restricted audit artifacts.

Set `security.redaction_level: strict` for regulated environments that need content names, query names, owner metadata, URLs, folder paths, and labels redacted from public artifacts.

## Exit Codes

- `0`: success
- `1`: validation failed
- `2`: configuration error
- `3`: authentication/authorization error
- `4`: Omni API error
- `5`: security policy violation
- `6`: internal tool error

## Security Notes

- Do not place Omni API keys in `.omniflow.yml`, `.omni/flow.json`, or PR metadata.
- Use least-privilege Omni tokens for CI.
- Keep `security.allow_raw_response_output` disabled unless debugging in a controlled environment.
- Use `security.redact_document_names: true` in stricter environments.
- GitHub Actions must avoid exposing secrets to unsafe fork PRs.

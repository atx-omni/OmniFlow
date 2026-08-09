# OmniFlow Troubleshooting

Start with the exact exit code and the redacted public report. Do not paste an API key, raw Omni payload, private model YAML, customer URLs, or restricted artifacts into an issue.

## Quick Diagnostic

From a trusted local checkout, an administrator can make `OMNI_API_KEY` available through the organization's approved local secret-injection method, then run:

```bash
omniflow doctor --auto
```

Do not put the key in command history or a local file. Remove it from the process environment when finished. `doctor` validates model discovery, API access, branch resolution where applicable, and Omni Git configuration metadata when the token permits it.

## Common Results

### The Pull Request Was Skipped

**Message:** The policy decision is `skipped`.

This is expected when changed files do not belong to a registered Omni model path and no Omni context marker is present. dbt-only and application-only pull requests should not fail because OmniFlow is installed.

If the pull request contains Omni files, verify that `model_path` in `.omni/flow.json` is the exact repository prefix for those files.

### Missing `OMNI_API_KEY`

Confirm that:

- The secret is named exactly `OMNI_API_KEY`.
- The secret is available to the repository or selected environment.
- The workflow references the secret only through its environment.
- The pull request comes from a branch in the same repository.

Fork pull requests intentionally do not receive the secret. Forked Omni changes fail closed; move the reviewed change to a same-repository branch for validation.

### Trusted `.omni/flow.json` Is Missing

The file must exist on the pull request's protected base branch. Adding it only to the proposed pull-request branch is not sufficient because OmniFlow refuses to trust model hosts supplied by unmerged code.

Check the filename, JSON syntax, `version: 1`, and the pull request's target branch.

### Omni Files Are Outside Every Registered Model Path

Update `.omni/flow.json` on the protected base branch so each Omni model's `model_path` matches its repository directory. Do not broaden a path to hide a routing error. Multi-model repositories should use one unique path per model.

### Omni Branch Could Not Be Resolved

Confirm that:

- The pull request was created from the intended Omni model branch.
- The GitHub head branch matches the Omni branch created through Git integration.
- The API key's Omni user can list model branches.
- The `model_id` identifies the base model rather than an unrelated model or old branch.

### Omni Git Configuration Mismatch

`omniflow doctor --auto` compares configured model path, base branch, provider, and repository URL with Omni when the API allows it. Correct the stale value in `.omni/flow.json` or the Omni Git integration. Do not disable the comparison without resolving which system is authoritative.

### HTTP 401 Or 403

- `401` usually means the API key is missing, invalid, or no longer active.
- `403` usually means the Omni user behind the key lacks access to the requested model or endpoint.

Use the least privilege needed for enabled checks. The optional dbt exposures endpoint can require more permission than core validation.

### Dependency Coverage Gap

OmniFlow fails closed by default when it cannot complete a downstream reference search for a breaking semantic element. This prevents an API or permission failure from being reported as "nothing depends on this field."

Investigate the associated Omni API status, model access, and element identifier. Change `contracts.fail_on.coverage_gaps` only after the governance owner accepts the reduced assurance.

### No Pull-Request Comment

Confirm that:

- The workflow has `pull-requests: write` permission.
- The event is `pull_request_target`.
- `.omniflow/public/report.md` was produced.
- Repository or organization policy does not block GitHub Actions from commenting.

The validation result is still available in the check and public artifact even if commenting fails.

### SARIF Upload Failed

Confirm that the workflow has `security-events: write` permission and that GitHub code scanning is available for the repository. The official example marks SARIF upload as non-blocking so a platform feature limitation does not hide the OmniFlow validation result.

### Package Installation Failed

Confirm that the workflow:

- Uses Python 3.11, 3.12, or 3.13.
- Pins the OmniFlow action to a full commit SHA.
- Has outbound access to Python package dependencies.
- Has not replaced the trusted installation step with an unpinned branch install.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success or intentional non-Omni skip. |
| `1` | Validation or policy gate failed. |
| `2` | Configuration or discovery error. |
| `3` | Authentication or authorization error. |
| `4` | Omni API error. |
| `5` | Security policy violation. |
| `6` | Unexpected internal error. |

If the issue remains, follow [Support](../SUPPORT.md) and share only redacted public evidence.

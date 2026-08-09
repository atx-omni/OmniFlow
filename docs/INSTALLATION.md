# Install OmniFlow In A GitHub Repository

This guide takes a repository from no OmniFlow setup to its first validated pull request. OmniFlow runs entirely in the customer's GitHub Actions account and calls the customer's Omni tenant directly. There is no OmniFlow service to install in Omni.

## Before You Start

Confirm that you have:

- An Omni model connected to the target GitHub repository through Omni Git integration.
- Omni Branch Mode enabled for the model.
- Permission to add repository files, GitHub Actions secrets, and branch protection rules.
- An Omni API key whose Omni user can read model branches and YAML, validate the model and content, and retrieve content metadata.
- The repository's protected base branch name, usually `main`.

The optional dbt exposures check requires the additional Omni permission documented by the [dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures).

## Step 1: Collect The Non-Secret Model Details

Collect these values for each Omni model connected to the repository:

| Value | Where it comes from |
| --- | --- |
| `base_url` | The Omni tenant origin, such as `https://company.omniapp.co`, with no page path. |
| `model_id` | The Omni model identifier from the model URL, Models API, or an Omni administrator. |
| `model_path` | The repository folder configured for that model in Omni Git integration. Use `.` only when the model files live at the repository root. |
| `base_branch` | The protected Git branch Omni targets, usually `main`. |
| `web_url` | The GitHub repository URL. |

You do not configure a per-pull-request branch name. OmniFlow discovers it from the GitHub pull request and resolves the corresponding Omni branch through the API.

## Step 2: Add Trusted Model Metadata

Create `.omni/flow.json` in the customer repository. Start from [the checked-in example](../.omni/flow.example.json):

```json
{
  "version": 1,
  "models": [
    {
      "base_url": "https://company.omniapp.co",
      "model_id": "00000000-0000-0000-0000-000000000000",
      "model_path": "omni/sales_model",
      "base_branch": "main",
      "git_provider": "github",
      "web_url": "https://github.com/company/analytics"
    }
  ]
}
```

For multiple models, add one object per model. Every `model_id` and `model_path` must be unique.

This file contains routing metadata, not credentials. Commit it to the protected base branch before testing an Omni-created pull request. OmniFlow deliberately reads this file from the trusted base branch so a pull request cannot redirect the API key to another host.

## Step 3: Add The Omni API Key To GitHub

In the customer repository:

1. Open **Settings**.
2. Open **Secrets and variables**, then **Actions**.
3. Select **New repository secret**.
4. Name the secret `OMNI_API_KEY`.
5. Enter the least-privilege Omni API key and save it.

Never put the key in `.omni/flow.json`, `.omniflow.yml`, a workflow file, a pull request, or an issue. OmniFlow rejects secret-like keys in configuration and redacts secret values from errors and reports.

Organization-level secrets also work when access is explicitly granted to the customer repository.

## Step 4: Add The GitHub Workflow

1. Create `.github/workflows/omniflow.yml` in the customer repository.
2. Copy the complete [workflow example](../.github/workflow-examples/omniflow.yml) into that file.
3. Replace both occurrences of `<pinned-commit-sha>` with the same reviewed, full 40-character OmniFlow commit SHA.
4. If the protected branch is not `main`, update `branches: [main]` in the workflow.
5. Commit the workflow through the customer's normal review process.

The action reference should look like this:

```yaml
uses: atx-omni/OmniFlow@0123456789abcdef0123456789abcdef01234567
```

Do not use `@main`, a floating tag, or an unpinned GitHub branch. During controlled alpha, the action installs directly from the pinned checkout. A signed PyPI release can be selected later through the action's `version` input.

The example workflow uses `pull_request_target` so it can read policy from the trusted base branch. It never checks out or executes proposed pull-request code. Do not add a pull-request-head checkout or execute scripts from the proposed branch in this privileged workflow.

## Step 5: Decide Whether To Add Policy

`.omniflow.yml` is optional. With no policy file, OmniFlow enables model validation, content validation, semantic lint, semantic diff, downstream contracts, and all four report formats using secure defaults.

To customize policy:

1. Copy [`.omniflow.example.yml`](../.omniflow.example.yml) to `.omniflow.yml` in the customer repository.
2. Review every changed gate with the model owners.
3. Commit the policy to the protected base branch.

See [Configuration](CONFIGURATION.md) for every supported setting. Do not add API keys or other secrets to policy.

## Step 6: Merge The Setup Into The Base Branch

The trusted workflow and `.omni/flow.json` must already exist on the protected base branch before they can validate an Omni-created pull request. Merge the setup through the repository's ordinary review process.

The initial setup pull request may not run OmniFlow because the trusted workflow does not exist on the base branch yet. This is expected.

## Step 7: Run The First Omni Pull Request

1. In Omni, create a model branch.
2. Make a harmless semantic change that is easy to review, such as adding a missing description.
3. Use Omni's **Create pull request** flow.
4. Open the resulting GitHub pull request.
5. Confirm the **OmniFlow** workflow starts.
6. Review the OmniFlow pull-request comment, annotations, and public evidence artifact.
7. Confirm the run selected the expected model and Omni branch.

Expected behavior:

- An Omni change runs model, content, lint, diff, and downstream contract checks.
- A non-Omni pull request finishes successfully with a `skipped` policy decision.
- An Omni change from a fork fails closed because the secret is intentionally withheld.
- A referenced breaking change fails the check and identifies affected downstream content when Omni returns that metadata.

During controlled alpha, do not make OmniFlow the only merge signal until this live test has passed against the customer's current Omni model and permissions.

## Step 8: Protect The Base Branch

After the first successful run:

1. Open the GitHub branch protection or ruleset for the base branch.
2. Require pull requests and the customer's normal approvals.
3. Add the OmniFlow status check, normally shown as `OmniFlow / omniflow`, as required.
4. Keep the workflow file and `.omni/flow.json` owned by trusted maintainers or CODEOWNERS if the repository uses them.
5. Confirm Omni's pull-request webhook is configured so approved merges remain synchronized with Omni.

OmniFlow validates and reports; it does not merge the pull request or write model YAML. After approval and merge, Omni Git integration performs the configured promotion behavior.

## Step 9: Verify The Installation

The installation is ready for controlled alpha use when all of these are true:

- [ ] `.omni/flow.json` is on the protected base branch.
- [ ] `.github/workflows/omniflow.yml` is on the protected base branch.
- [ ] Both action references use the same full OmniFlow commit SHA.
- [ ] `OMNI_API_KEY` exists only in GitHub Actions secrets.
- [ ] A real Omni-created pull request selected the expected model and branch.
- [ ] Model, content, lint, diff, and downstream checks completed.
- [ ] Public reports contain no credentials or restricted values.
- [ ] The required status check and reviewer approvals are configured.
- [ ] Omni's post-merge Git integration behavior has been verified separately.

For setup failures, continue with [Troubleshooting](TROUBLESHOOTING.md). For safe diagnostic sharing, see [Support](../SUPPORT.md).

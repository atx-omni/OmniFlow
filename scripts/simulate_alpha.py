#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODEL_ID = "model-1"
BRANCH_ID = "branch-1"
BRANCH_NAME = "feature/omniflow-alpha"


BASE_FILES = {
    "views/orders.view": """name: orders
fields:
  id:
    type: number
    primary_key: true
    description: Order ID
  revenue:
    type: number
    description: Revenue
    aggregate_type: sum
""",
    "relationships/relationships.yaml": """- join_from_view: orders
  join_to_view: order_items
  relationship_type: many_to_one
  on_sql: $${orders.id} = $${order_items.order_id}
""",
    "topics/sales.topic": """name: sales
label: Sales
base_view: orders
""",
}


HEAD_FILES = {
    "views/orders.view": """name: orders
fields:
  id:
    type: number
    primary_key: true
    description: Order ID
""",
    "relationships/relationships.yaml": """- join_from_view: orders
  join_to_view: order_items
  relationship_type: one_to_many
  on_sql: $${orders.id} = $${order_items.order_id}
""",
    "topics/sales.topic": """name: sales
label: Sales
base_view: orders
""",
}


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    expected_exit: int
    changed_files: str
    marker: dict[str, Any] | None = None
    server_mode: str = "normal"
    config: str | None = None
    include_api_key: bool = True


SCENARIOS = [
    Scenario(
        name="skip_non_omni_pr",
        description="dbt-only PR should skip cleanly",
        expected_exit=0,
        changed_files="models/marts/fct_orders.sql",
        server_mode="unused",
    ),
    Scenario(
        name="fork_non_omni_without_secret",
        description="fork dbt-only PR should skip without receiving the Omni secret",
        expected_exit=0,
        changed_files="models/marts/fct_orders.sql",
        server_mode="unused",
        include_api_key=False,
    ),
    Scenario(
        name="fork_omni_without_secret",
        description="fork Omni PR should fail closed when the Omni secret is withheld",
        expected_exit=2,
        changed_files="omni/model/views/orders.view",
        server_mode="unused",
        include_api_key=False,
    ),
    Scenario(
        name="contract_failure",
        description="deleted referenced field should fail with public artifacts",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
    ),
    Scenario(
        name="strict_redaction",
        description="strict public reports should redact content names and owner metadata",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
        config="""security:
  redaction_level: strict
checks:
  dbt_exposures:
    enabled: true
""",
    ),
    Scenario(
        name="missing_branch",
        description="branch name without branch ID should fail closed and still write artifacts",
        expected_exit=2,
        changed_files="omni/model/views/orders.view",
        server_mode="missing_branch",
    ),
    Scenario(
        name="bad_marker_base_url",
        description="PR marker with base_url should be rejected as a security violation",
        expected_exit=5,
        changed_files="docs/readme.md",
        marker={"base_url": "https://evil.example", "model_id": MODEL_ID, "branch_name": BRANCH_NAME},
    ),
    Scenario(
        name="exposures_unavailable_warning",
        description="dbt exposures API failure should warn but not change contract failure policy",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
        server_mode="exposures_403",
        config="""checks:
  dbt_exposures:
    enabled: true
    fail_on_unavailable: false
""",
    ),
]


class FakeOmniState:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests: list[dict[str, Any]] = []


def make_handler(state: FakeOmniState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            state.requests.append({"path": parsed.path, "query": query})
            try:
                payload, status = route_request(state, parsed.path, query)
            except Exception as exc:  # noqa: BLE001 - simulation server should surface unexpected behavior.
                payload, status = {"error": str(exc)}, 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

    return Handler


def route_request(state: FakeOmniState, path: str, query: dict[str, list[str]]) -> tuple[Any, int]:
    if path == "/api/v1/models":
        records = []
        if state.mode != "missing_branch":
            records.append({"id": BRANCH_ID, "modelKind": "BRANCH", "baseModelId": MODEL_ID, "name": BRANCH_NAME})
        return {"records": records, "pageInfo": {}}, 200
    if path == f"/api/v1/models/{MODEL_ID}/git":
        return {
            "modelPath": "omni/model",
            "baseBranch": "main",
            "gitServiceProvider": "github",
            "webUrl": "https://github.com/atx-omni/simulated",
            "branchPerPullRequest": True,
            "gitFollower": False,
            "requirePullRequest": True,
        }, 200
    if path == f"/api/v1/models/{MODEL_ID}/validate":
        return [], 200
    if path == f"/api/v1/models/{MODEL_ID}/yaml":
        files = HEAD_FILES if query.get("branchId") == [BRANCH_ID] else BASE_FILES
        return {"files": files, "checksums": {name: f"checksum-{name}" for name in files}}, 200
    if path == f"/api/v1/models/{MODEL_ID}/content-validator":
        if query.get("find") == ["orders.revenue"] and query.get("find_type") == ["FIELD"]:
            return content_payload("Executive Revenue", "alice@example.com"), 200
        return {"content": []}, 200
    if path == "/api/v1/content":
        return {"records": [{"identifier": "dash-1", "labels": [{"name": "Verified"}]}], "pageInfo": {}}, 200
    if path == f"/api/v1/models/{MODEL_ID}/dbt-exposures":
        if state.mode == "exposures_403":
            return {"error": "forbidden"}, 403
        return {
            "records": [
                {
                    "dashboard_identifier": "dash-1",
                    "deduplication_name": "executive_revenue",
                    "exposure": {
                        "name": "executive_revenue",
                        "label": "Executive Revenue",
                        "type": "dashboard",
                        "url": "https://omni.example/dashboards/dash-1",
                        "owner": {"name": "Alice", "email": "alice@example.com"},
                        "depends_on": ["model.orders"],
                    },
                }
            ],
            "pageInfo": {"hasNextPage": False},
        }, 200
    return {"error": f"Unhandled fake Omni path: {path}"}, 404


def content_payload(name: str, email: str) -> dict[str, Any]:
    return {
        "content": [
            {
                "document_id": "dash-1",
                "identifier": "dash-1",
                "type": "dashboard",
                "name": name,
                "url": "https://omni.example/dashboards/dash-1",
                "owner": {"name": "Alice", "email": email},
                "folder": {"name": "Leadership", "path": "/Executive/Leadership"},
                "labels": [{"name": "Executive"}],
                "queries_and_issues": [
                    {
                        "query_presentation_id": "query-1",
                        "query_name": "Revenue by Month",
                        "issues": [{"message": "Field orders.revenue was not found"}],
                    }
                ],
            }
        ]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OmniFlow alpha simulations against a fake Omni API")
    parser.add_argument("--scenario", choices=[scenario.name for scenario in SCENARIOS], action="append")
    parser.add_argument("--keep-workdirs", action="store_true")
    args = parser.parse_args(argv)
    wanted = set(args.scenario or [scenario.name for scenario in SCENARIOS])
    selected = [scenario for scenario in SCENARIOS if scenario.name in wanted]

    failures = []
    for scenario in selected:
        result = run_scenario(scenario, keep_workdir=args.keep_workdirs)
        print_result(result)
        if not result["passed"]:
            failures.append(result)
    if failures:
        print(f"\n{len(failures)} simulation(s) failed.", file=sys.stderr)
        return 1
    print(f"\nAll {len(selected)} simulation(s) passed.")
    return 0


def run_scenario(scenario: Scenario, *, keep_workdir: bool) -> dict[str, Any]:
    tmp_ctx = tempfile.TemporaryDirectory(prefix=f"omniflow-{scenario.name}-")
    tmp = Path(tmp_ctx.name)
    state = FakeOmniState(scenario.server_mode)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        repo = tmp / "repo"
        repo.mkdir()
        setup_repo(repo, base_url=base_url, scenario=scenario)
        completed = run_omniflow(repo, scenario)
        artifacts = inspect_artifacts(repo)
        passed, errors = assert_result(scenario, completed.returncode, artifacts)
        return {
            "name": scenario.name,
            "description": scenario.description,
            "passed": passed,
            "errors": errors,
            "exit_code": completed.returncode,
            "expected_exit": scenario.expected_exit,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "workdir": str(repo),
            "artifacts": artifacts,
            "requests": state.requests,
        }
    finally:
        server.shutdown()
        server.server_close()
        if keep_workdir:
            print(f"Kept workdir for {scenario.name}: {tmp}")
        else:
            tmp_ctx.cleanup()


def setup_repo(repo: Path, *, base_url: str, scenario: Scenario) -> None:
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "omniflow@example.com"], cwd=repo)
    run(["git", "config", "user.name", "OmniFlow Simulation"], cwd=repo)
    (repo / ".omni").mkdir()
    write_json(
        repo / ".omni/flow.json",
        {
            "version": 1,
            "models": [
                {
                    "base_url": base_url,
                    "model_id": MODEL_ID,
                    "model_path": "omni/model",
                    "base_branch": "main",
                    "git_provider": "github",
                    "web_url": "https://github.com/atx-omni/simulated",
                }
            ],
        },
    )
    (repo / "omni/model/views").mkdir(parents=True)
    (repo / "omni/model/views/orders.view").write_text(BASE_FILES["views/orders.view"], encoding="utf-8")
    config = (
        scenario.config
        or """reporting:
  formats: [json, markdown, sarif, junit]
security:
  redaction_level: standard
"""
    )
    (repo / ".omniflow.yml").write_text(config, encoding="utf-8")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    run(["git", "checkout", "-q", "-b", BRANCH_NAME], cwd=repo)
    apply_changed_files(repo, scenario.changed_files)
    event = {"pull_request": {"body": marker_body(scenario), "number": 1}}
    write_json(repo / "event.json", event)


def apply_changed_files(repo: Path, changed_files: str) -> None:
    for raw in changed_files.splitlines():
        path = raw.strip()
        if not path:
            continue
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith(".sql"):
            target.write_text("select 1 as id\n", encoding="utf-8")
        elif path.endswith(".md"):
            target.write_text("# docs\n", encoding="utf-8")
        elif path.endswith(".view"):
            target.write_text(HEAD_FILES["views/orders.view"], encoding="utf-8")
        else:
            target.write_text("changed\n", encoding="utf-8")


def marker_body(scenario: Scenario) -> str:
    if not scenario.marker:
        return ""
    return f"<!-- omniflow-context {json.dumps(scenario.marker, separators=(',', ':'))} -->"


def run_omniflow(repo: Path, scenario: Scenario) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("OMNI_API_KEY", None)
    env.update(
        {
            "PYTHONPATH": str(SRC),
            "GITHUB_HEAD_REF": BRANCH_NAME,
            "GITHUB_BASE_REF": "main",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": str(repo / "event.json"),
            "OMNIFLOW_CHANGED_FILES": scenario.changed_files,
        }
    )
    if scenario.include_api_key:
        env["OMNI_API_KEY"] = "simulation-secret"  # pragma: allowlist secret
    return subprocess.run(
        [sys.executable, "-m", "omniflow.cli", "run", "--auto", "--config", ".omniflow.yml"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def inspect_artifacts(repo: Path) -> dict[str, Any]:
    root = repo / ".omniflow"
    artifacts: dict[str, Any] = {"exists": root.exists(), "files": []}
    if not root.exists():
        return artifacts
    artifacts["files"] = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
    for relative in ("report.json", "public/report.json", "public/report.md", "artifact-manifest.json"):
        path = root / relative
        if path.exists():
            artifacts[relative] = path.read_text(encoding="utf-8")
            if relative.endswith(".json"):
                try:
                    artifacts[f"{relative}:json"] = json.loads(artifacts[relative])
                except json.JSONDecodeError:
                    pass
    return artifacts


def assert_result(scenario: Scenario, exit_code: int, artifacts: dict[str, Any]) -> tuple[bool, list[str]]:
    errors = []
    if exit_code != scenario.expected_exit:
        errors.append(f"expected exit {scenario.expected_exit}, got {exit_code}")
    if not artifacts.get("exists"):
        errors.append("missing .omniflow artifacts")
    if "public/report.json" not in artifacts.get("files", []):
        errors.append("missing public/report.json")
    public_text = artifacts.get("public/report.json", "")
    if (
        "simulation-secret" in public_text
        or "alice@example.com" in public_text
        or "https://omni.example/dashboards" in public_text
    ):
        errors.append("public report leaked secret/email/dashboard URL")
    if scenario.name == "strict_redaction" and "Executive Revenue" in public_text:
        errors.append("strict public report leaked content name")
    if scenario.name == "bad_marker_base_url" and "security policy violation" not in public_text.lower():
        errors.append("bad marker scenario did not report security policy violation")
    if scenario.name == "skip_non_omni_pr":
        report = artifacts.get("public/report.json:json", {})
        if report.get("policy_decision") != "skipped":
            errors.append("non-Omni PR did not produce skipped policy decision")
    if scenario.name == "fork_non_omni_without_secret":
        report = artifacts.get("public/report.json:json", {})
        if report.get("policy_decision") != "skipped":
            errors.append("fork non-Omni PR did not skip without the Omni secret")
    if scenario.name == "fork_omni_without_secret":
        report = artifacts.get("public/report.json:json", {})
        if report.get("policy_decision") != "fail" or report.get("exit_code") != 2:
            errors.append("fork Omni PR did not fail closed without the Omni secret")
    return not errors, errors


def print_result(result: dict[str, Any]) -> None:
    status = "PASS" if result["passed"] else "FAIL"
    print(f"[{status}] {result['name']}: {result['description']}")
    print(f"  exit: {result['exit_code']} expected: {result['expected_exit']}")
    print(f"  artifacts: {', '.join(result['artifacts'].get('files', [])[:8])}")
    for error in result["errors"]:
        print(f"  error: {error}")
    if not result["passed"]:
        print(f"  stdout: {result['stdout'][-1000:]}")
        print(f"  stderr: {result['stderr'][-1000:]}")
        print(f"  workdir: {result['workdir']}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(cmd: list[str], *, cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())

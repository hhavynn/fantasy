"""The CI docs-only classifier (fantasy-intel/scripts/ci_changes.py, the `changes` job in .github/workflows/ci.yml)."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = SCRIPTS_DIR.parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import ci_changes
from ci_changes import classify, decide, is_docs_path

# ---- classify(): the path rules ---------------------------------------------------------------------------------

@pytest.mark.parametrize("paths", [
    ["docs/research/186-player-news-sources.md"],
    ["docs/qa/release-review.md", "docs/superpowers/plans/website-polish/plan.md"],
    ["README.md"],
    ["docs/new-note.md"],
])
def test_docs_only(paths):
    docs_only, reason = classify(paths)
    assert docs_only and "docs-only" in reason


@pytest.mark.parametrize("paths", [
    # frontend-only
    ["fantasy-intel/frontend/app/page.tsx"],
    ["fantasy-intel/frontend/PRODUCT.md"],
    ["fantasy-intel/frontend/package-lock.json"],
    # backend-only
    ["fantasy-intel/backend/app/analytics/trades.py"],
    ["fantasy-intel/backend/uv.lock"],
    ["fantasy-intel/backend/tests/test_trades.py"],
    # security-sensitive: migrations, auth, RLS, /v2
    ["fantasy-intel/supabase/migrations/20261020_rls.sql"],
    ["fantasy-intel/backend/app/control_plane/auth.py"],
    ["fantasy-intel/backend/app/api/v2.py"],
    # workflow-only and other config
    [".github/workflows/ci.yml"],
    [".github/pull_request_template.md"],
    ["fantasy-intel/scripts/ci_changes.py"],
    # agent policy and docs that tests read
    ["AGENTS.md"], ["CLAUDE.md"], ["GEMINI.md"],
    [".claude/skills/fantasy-verify/references/architecture-map.md"],
    ["docs/agent-workflow.md"],
    ["docs/agent-security-review.md"],
    ["docs/multileague/provider-capability-matrix.md"],
    ["fantasy-intel/docs/research/rb_projection_diagnostics.md"],
    ["fantasy-intel/docs/shadow_promotion.md"],
    # docs/ but not Markdown
    ["docs/screenshots/weekly-recap/desktop-dark.png"],
    # mixed
    ["docs/research/note.md", "fantasy-intel/backend/app/main.py"],
    ["README.md", ".github/workflows/ci.yml"],
    # odd names
    ["docs/a\nb.md"], [" docs/a.md"], ["/docs/a.md"], ["docs/../fantasy-intel/x.md"],
])
def test_full_ci(paths):
    docs_only, reason = classify(paths)
    assert not docs_only and "full CI" in reason


def test_empty_diff_is_full_ci():
    assert classify([]) == (False, "empty diff: full CI")


def test_non_pr_events_are_full_ci():
    for event in ("push", "workflow_dispatch", "schedule", ""):
        docs_only, reason = decide(event, "abc", "def")
        assert not docs_only and "full CI" in reason


def test_missing_shas_are_full_ci():
    assert decide("pull_request", "", "abc")[0] is False
    assert decide("pull_request", "abc", "")[0] is False


# ---- decide() against a real repository: merge base, renames, deletions ----------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    for rel, text in {"docs/guide.md": "a\n", "fantasy-intel/backend/app/main.py": "x = 1\n",
                      "README.md": "r\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _branch(repo, name, change):
    _git(repo, "checkout", "-q", "-b", name, "main")
    change(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", name, "--allow-empty")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return head


def test_git_docs_only_pr(repo):
    head = _branch(repo, "docs", lambda r: (r / "docs/guide.md").write_text("b\n"))
    assert decide("pull_request", _git(repo, "rev-parse", "main"), head)[0] is True


def test_git_backend_pr(repo):
    head = _branch(repo, "be", lambda r: (r / "fantasy-intel/backend/app/main.py").write_text("x = 2\n"))
    assert decide("pull_request", "main", head)[0] is False


def test_git_uses_the_merge_base_not_the_base_tip(repo):
    head = _branch(repo, "docs", lambda r: (r / "docs/guide.md").write_text("b\n"))
    (repo / "fantasy-intel/backend/app/main.py").write_text("x = 3\n")   # main moves on after the branch point
    _git(repo, "commit", "-qam", "main moves")
    assert decide("pull_request", _git(repo, "rev-parse", "main"), head)[0] is True


def test_git_rename_of_code_into_docs_is_full_ci(repo):
    head = _branch(repo, "mv", lambda r: _git(r, "mv", "fantasy-intel/backend/app/main.py", "docs/main.md"))
    docs_only, reason = decide("pull_request", "main", head)
    assert not docs_only and "fantasy-intel/backend/app/main.py" in reason


def test_git_rename_within_docs_is_docs_only(repo):
    def move(r):
        (r / "docs/moved").mkdir()
        _git(r, "mv", "docs/guide.md", "docs/moved/guide.md")
    head = _branch(repo, "mv", move)
    assert decide("pull_request", "main", head)[0] is True


def test_git_deletion(repo):
    head = _branch(repo, "rm-doc", lambda r: _git(r, "rm", "-q", "docs/guide.md"))
    assert decide("pull_request", "main", head)[0] is True
    head = _branch(repo, "rm-code", lambda r: _git(r, "rm", "-q", "fantasy-intel/backend/app/main.py"))
    assert decide("pull_request", "main", head)[0] is False


def test_git_empty_diff_is_full_ci(repo):
    head = _branch(repo, "empty", lambda r: None)
    assert decide("pull_request", "main", head) == (False, "empty diff: full CI")


def test_git_unknown_sha_is_full_ci(repo):
    docs_only, reason = decide("pull_request", "main", "0" * 40)
    assert not docs_only and "undeterminable" in reason


def test_git_non_utf8_path_is_full_ci(repo):
    def add(r):
        (r / os.fsdecode(b"docs/caf\xe9.md")).write_bytes(b"latin-1 name\n")     # not valid UTF-8
    head = _branch(repo, "latin1", add)
    docs_only, reason = decide("pull_request", "main", head)
    assert not docs_only and "full CI" in reason
    assert is_docs_path(os.fsdecode(b"docs/caf\xe9.md")) is False


def test_main_never_prints_unprintable_reason(repo, capsys):
    head = _branch(repo, "latin1", lambda r: (r / os.fsdecode(b"docs/\xff.md")).write_bytes(b"x\n"))
    assert ci_changes.main(["--event", "pull_request", "--base", "main", "--head", head]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "docs_only=false" and len(lines) == 2 and all(line.isprintable() for line in lines)


def test_main_prints_github_output_lines(repo, capsys):
    head = _branch(repo, "docs", lambda r: (r / "docs/guide.md").write_text("b\n"))
    assert ci_changes.main(["--event", "pull_request", "--base", "main", "--head", head]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "docs_only=true" and lines[1].startswith("reason=") and len(lines) == 2


# ---- the deny-list stays in step with what tests and scripts read ----------------------------------------------

_LITERAL = re.compile(r"""["'`]((?:docs|\.claude)/[\w./-]+\.md)["'`]""")
_JOIN = re.compile(r"""["']docs["']((?:\s*/\s*["'][\w.-]+["'])+)""")


def test_every_root_doc_that_tests_or_scripts_name_is_not_docs_only():
    sources = [*(REPO_ROOT / "fantasy-intel/backend/tests").glob("*.py"),
               *(REPO_ROOT / "fantasy-intel/backend/scripts").glob("*.py"),
               *(REPO_ROOT / "fantasy-intel/scripts").glob("*.py"),
               *(REPO_ROOT / "fantasy-intel/frontend/e2e").glob("*.ts")]
    named = set()
    for src in sources:
        if src.name == "test_ci_changes.py":
            continue
        text = src.read_text(errors="replace")
        named.update(_LITERAL.findall(text))
        for parts in _JOIN.findall(text):
            named.add("docs/" + "/".join(re.findall(r"""["']([\w.-]+)["']""", parts)))
    root_docs = {p for p in named if p.endswith(".md") and (REPO_ROOT / p).is_file()}
    assert "docs/agent-workflow.md" in root_docs                           # the scan itself still finds readers
    assert "docs/multileague/provider-capability-matrix.md" in root_docs
    assert not [p for p in root_docs if is_docs_path(p)], "add these to NOT_DOCS_ONLY in scripts/ci_changes.py"


# ---- whole-repository readers: they read Markdown too, so the docs-only lane must still run them ----------------

# Test files that read every tracked file (or rglob from the repository root), Markdown included. Each must run in the
# docs-scan job of ci.yml, because a docs-only change skips the backend shards.
WHOLE_REPO_SCANS = {"tests/test_no_credential_leaks.py"}
_WHOLE_REPO = re.compile(r"ls-files|(?:REPO_ROOT|REPO|parents\[3\]\)?)\s*\)?\.(?:r?glob|walk)\(")


def test_every_whole_repository_reader_is_listed():
    readers = set()
    for src in [*(REPO_ROOT / "fantasy-intel/backend/tests").glob("*.py"), *(REPO_ROOT / "fantasy-intel/scripts").glob("*.py"),
                *(REPO_ROOT / "fantasy-intel/backend/scripts").glob("*.py")]:
        if src.name not in ("test_ci_changes.py", "agent_check.py") and _WHOLE_REPO.search(src.read_text(errors="replace")):
            readers.add(src.relative_to(REPO_ROOT / "fantasy-intel/backend").as_posix()
                        if src.is_relative_to(REPO_ROOT / "fantasy-intel/backend") else src.relative_to(REPO_ROOT).as_posix())
    assert readers == WHOLE_REPO_SCANS, "a new whole-repository reader: run it in ci.yml's docs-scan job and list it here"


# ---- the workflow wiring (.github/workflows/ci.yml), executed with the real step scripts -------------------------

WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
HEAVY_JOBS = ("backend-shard", "social", "frontend", "e2e-suite")
AGGREGATES = {"backend": "backend-shard", "e2e": "e2e-suite"}


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _step(job, name_part):
    return next(s for s in job["steps"] if name_part in s.get("name", ""))


def test_heavy_jobs_skip_only_on_docs_only_and_run_when_classification_fails(jobs):
    for name in HEAVY_JOBS:
        assert jobs[name]["needs"] == "changes"
        assert jobs[name]["if"] == "${{ !cancelled() && needs.changes.outputs.docs_only != 'true' }}", name


def test_the_docs_only_lane_runs_every_whole_repository_scan(jobs):
    scan = jobs["docs-scan"]
    assert scan["if"] == "${{ !cancelled() && needs.changes.outputs.docs_only == 'true' }}"
    command = _step(scan, "credentials")["run"]
    leaks = (REPO_ROOT / "fantasy-intel/backend/tests/test_no_credential_leaks.py").read_text()
    for node in ("test_no_credentials_are_committed_to_the_repository", "test_the_env_example_files_ship_empty_credentials",
                 "test_the_scanner_catches_each_credential_shape_and_honours_the_allowlist"):
        assert f"tests/test_no_credential_leaks.py::{node}" in command and f"def {node}(" in leaks, node
    for path in WHOLE_REPO_SCANS:
        assert path in command, path
    for agg in AGGREGATES:
        assert "docs-scan" in jobs[agg]["needs"], agg


def _run_aggregate(jobs, agg, changes, docs_only, scan, suites, tmp_path):
    env = {"PATH": os.environ["PATH"], "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), "CHANGES": changes,
           "DOCS_ONLY": docs_only, "DOCS_SCAN": scan, "SHARDS": suites, "SUITES": suites}
    return subprocess.run(["bash", "-e", "-c", jobs[agg]["steps"][0]["run"]], env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("changes,docs_only,scan,suites,green", [
    ("success", "true", "success", "skipped", True),       # docs-only, scan passed
    ("success", "true", "failure", "skipped", False),      # docs-only, a credential in the docs
    ("success", "true", "cancelled", "skipped", False),
    ("success", "true", "skipped", "skipped", False),      # the scan never ran
    ("success", "false", "skipped", "success", True),      # full CI
    ("success", "false", "skipped", "failure", False),
    ("success", "false", "skipped", "cancelled", False),   # a cancelled leg is red (always(), not !cancelled())
    ("success", "false", "skipped", "skipped", False),
    ("failure", "", "skipped", "success", True),           # classification failed: full CI ran and passed
    ("failure", "", "skipped", "failure", False),
])
def test_the_aggregates(jobs, tmp_path, changes, docs_only, scan, suites, green):
    for agg in AGGREGATES:
        assert jobs[agg]["if"] == "always()"
        result = _run_aggregate(jobs, agg, changes, docs_only, scan, suites, tmp_path)
        assert (result.returncode == 0) is green, (agg, result.stdout, result.stderr)
        if green and docs_only == "true":
            assert "docs-only change: backend/frontend/E2E not applicable, not run" in (tmp_path / "summary").read_text()


@pytest.fixture
def runner(tmp_path_factory):
    return tmp_path_factory.mktemp("runner")       # RUNNER_TEMP and the output files, outside the repository


def _classify_step(jobs, repo, tmp_path, event, base, head):
    out, summary = tmp_path / "out", tmp_path / "summary"
    out.write_text(""), summary.write_text("")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out),
           "GITHUB_STEP_SUMMARY": str(summary), "EVENT": event, "BASE_SHA": base, "HEAD_SHA": head}
    run = _step(jobs["changes"], "Classify")["run"]
    result = subprocess.run(["bash", "-e", "-c", run], cwd=repo, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines()), result.stdout


def _with_classifier(repo, text):
    (repo / "fantasy-intel/scripts").mkdir(parents=True, exist_ok=True)
    (repo / "fantasy-intel/scripts/ci_changes.py").write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "classifier")


def test_the_classify_step_runs_the_base_commits_classifier(jobs, repo, runner):
    _with_classifier(repo, (SCRIPTS_DIR / "ci_changes.py").read_text())
    base = _git(repo, "rev-parse", "main")
    docs = _branch(repo, "docs", lambda r: (r / "docs/guide.md").write_text("b\n"))
    assert _classify_step(jobs, repo, runner, "pull_request", base, docs)[0]["docs_only"] == "true"

    def loosen(r):     # the pull request rewrites the classifier to call everything docs-only, and touches code
        (r / "fantasy-intel/scripts/ci_changes.py").write_text('print("docs_only=true")\nprint("reason=mine")\n')
        (r / "fantasy-intel/backend/app/main.py").write_text("x = 9\n")
    loose = _branch(repo, "loose", loosen)
    verdict, _ = _classify_step(jobs, repo, runner, "pull_request", base, loose)
    assert verdict["docs_only"] == "false" and "fantasy-intel/" in verdict["reason"]


def test_the_classify_step_is_full_ci_without_a_base_classifier(jobs, repo, runner):
    docs = _branch(repo, "docs", lambda r: (r / "docs/guide.md").write_text("b\n"))
    verdict, _ = _classify_step(jobs, repo, runner, "pull_request", _git(repo, "rev-parse", "main"), docs)
    assert verdict == {"docs_only": "false", "reason": "no classifier at the base commit: full CI"}


def test_the_classify_step_is_full_ci_when_the_classifier_fails(jobs, repo, runner):
    _with_classifier(repo, "raise SystemExit(3)\n")
    docs = _branch(repo, "docs", lambda r: (r / "docs/guide.md").write_text("b\n"))
    verdict, stdout = _classify_step(jobs, repo, runner, "pull_request", _git(repo, "rev-parse", "main"), docs)
    assert verdict["docs_only"] == "false" and "::warning" in stdout


def test_the_classify_step_is_full_ci_for_pushes(jobs, repo, runner):
    _with_classifier(repo, (SCRIPTS_DIR / "ci_changes.py").read_text())
    for event in ("push", "workflow_dispatch"):
        verdict, _ = _classify_step(jobs, repo, runner, event, "", "")
        assert verdict == {"docs_only": "false", "reason": f"{event} event: full CI"}

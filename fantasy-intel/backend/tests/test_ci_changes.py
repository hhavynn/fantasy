"""The CI docs-only classifier (fantasy-intel/scripts/ci_changes.py, the `changes` job in .github/workflows/ci.yml)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

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

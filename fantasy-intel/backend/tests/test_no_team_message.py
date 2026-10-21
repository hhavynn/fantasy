"""The frontend recognizes "this page needs a team" by the API's message (components/TeamPerspective.needsTeam shows
the team picker instead of an error): every route that sends it must send exactly what the frontend matches."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "components" / "TeamPerspective.tsx"


def test_every_no_team_answer_is_one_the_team_picker_recognizes():
    pattern = re.search(r"/(No team is marked as yours)/", FRONTEND.read_text())
    assert pattern, "needsTeam's pattern moved: update this test with it"
    sent = set()
    for f in APP.rglob("*.py"):
        sent |= set(re.findall(r'"(No team is marked as yours[^"]*)"', f.read_text()))
    assert sent and all(m.startswith(pattern[1]) for m in sent), sent

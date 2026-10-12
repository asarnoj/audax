import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_self_description_matches_the_real_schedule():
    """how_i_work must never describe a schedule the agent doesn't actually run."""
    main = (ROOT / "agent" / "main.py").read_text()
    me = (ROOT / "agent" / "prompts" / "self.md").read_text()
    for job, label in (("manager", "Manager"), ("director_understand", "genomgång"), ("director_reflect", "reflektion")):
        m = re.search(rf'hour=(\d+), minute=(\d+).*id="{job}"', main)
        assert m, job
        assert f"{int(m.group(1)):02d}:{int(m.group(2)):02d}" in me, f"{label}: tiden i self.md stämmer inte med main.py"
    assert 'day_of_week="mon,wed,sat"' in main and "Mondays, Wednesdays and Saturdays" in me
    quiet = (ROOT / "config" / "settings.example.yaml").read_text()
    start, end = re.search(r'start: "(\d\d:\d\d)"\s+end: "(\d\d:\d\d)"', quiet).groups()
    assert f"{start}–{end}" in me

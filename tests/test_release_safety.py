import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".gitignore").read_text(encoding="utf-8")
    assert "case-set.json" in ignored
    assert "inputs/*" in ignored
    assert "outputs/*" in ignored
    if (root / ".git").exists():
        tracked = subprocess.run(
            ["git", "ls-files", "case-set.json", "inputs/*.json", "outputs/*.json"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert tracked.strip() == ""
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in root.rglob("*"))


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1

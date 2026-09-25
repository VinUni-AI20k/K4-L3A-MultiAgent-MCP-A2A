import subprocess
from pathlib import Path


def test_repository_tracks_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    # The README requires downloaded inputs in the working tree. Release safety
    # concerns the files published by Git, not ignored local runtime artifacts.
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    )
    tracked = [Path(name) for name in result.stdout.split("\0") if name]
    assert Path("case-set.json") not in tracked
    assert Path(".env") not in tracked
    assert not any(
        p.parts[0] in {"inputs", "outputs", "traces", "dist"} and p.name != ".gitkeep"
        for p in tracked
    )
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(forbidden.intersection(path.parts) for path in tracked)


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1

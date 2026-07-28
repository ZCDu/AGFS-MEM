from pathlib import Path
import tomllib


def test_redis_py_is_pinned_to_reviewed_version() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert "redis==6.4.0" in project["project"]["dependencies"]

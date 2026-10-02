"""The Space folder contains what the container needs, and nothing secret."""

from pathlib import Path

import pytest

from scripts.build_space import FILES, ROOT, build_space


@pytest.fixture
def project(tmp_path) -> Path:
    """A miniature copy of the repository layout."""
    root = tmp_path / "project"
    files = {
        "deploy/huggingface/README.md": "---\nsdk: docker\n---\n",
        "deploy/huggingface/Dockerfile": "FROM python:3.12-slim\n",
        "pyproject.toml": "[project]\n",
        "LICENSE": "MIT\n",
        "README.md": "# GitHub readme\n",
        ".env": "LLM_API_KEY=secret\n",
        "app/api.py": "",
        "app/static/index.html": "",
        "app/__pycache__/api.cpython-312.pyc": "",
        "app/.env": "LLM_API_KEY=secret\n",
        "data/lux/stops.geojson": "{}",
        "data/lux/SOURCES.json": "{}",
        "data/sample/stops.geojson": "{}",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def _tree(folder: Path) -> set[str]:
    return {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}


def test_space_contains_only_the_listed_files(project, tmp_path):
    out = tmp_path / "space"
    build_space(project, out)
    assert _tree(out) == {
        "README.md",
        "Dockerfile",
        "pyproject.toml",
        "LICENSE",
        "app/api.py",
        "app/static/index.html",
        "data/lux/stops.geojson",
        "data/lux/SOURCES.json",
    }
    # The Space gets its own README (with the YAML header), not the GitHub one.
    assert (out / "README.md").read_text().startswith("---\nsdk: docker")


def test_rebuild_removes_stale_files_but_keeps_git(project, tmp_path):
    out = tmp_path / "space"
    build_space(project, out)
    (out / ".git").mkdir()
    (out / ".git" / "config").write_text("[core]\n")
    (out / "app" / "removed.py").write_text("")
    build_space(project, out)
    assert not (out / "app" / "removed.py").exists()
    assert (out / ".git" / "config").exists()


def test_refuses_to_build_without_provenance(project, tmp_path):
    (project / "data/lux/SOURCES.json").unlink()
    with pytest.raises(FileNotFoundError, match="fetch_lux_data"):
        build_space(project, tmp_path / "space")


def test_space_files_exist_in_the_repository():
    for source in FILES.values():
        assert (ROOT / source).is_file(), source
    header = (ROOT / "deploy/huggingface/README.md").read_text(encoding="utf-8").split("---")[1]
    assert "sdk: docker" in header
    assert "app_port: 7860" in header
    dockerfile = (ROOT / "deploy/huggingface/Dockerfile").read_text(encoding="utf-8")
    assert '"--port", "7860"' in dockerfile
    assert "LLM_API_KEY=" not in dockerfile

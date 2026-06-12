from importlib.metadata import metadata


def test_project_metadata_is_public_release_ready() -> None:
    project = metadata("dani")
    keywords = set(project["Keywords"].split(","))
    classifiers = set(project.get_all("Classifier") or [])
    urls = set(project.get_all("Project-URL") or [])

    assert project["Summary"] == "Experimental local-first GitHub webhook automation for Gajae-Code and Codex agents"
    assert project["License-Expression"] == "MIT"
    assert {"github", "webhook", "automation", "codex", "gajae-code", "agent"} <= keywords
    assert "License :: OSI Approved :: MIT License" in classifiers
    assert "Framework :: FastAPI" in classifiers
    assert "Issues, https://github.com/NomaDamas/dani/issues" in urls

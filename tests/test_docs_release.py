from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_has_experimental_banner() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.startswith("# dani\n\n> [!WARNING]\n> Experimental project")


def test_readme_has_user_quickstart_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for expected in [
        "uv tool install dani",
        "dani register-repo owner/name /absolute/path/to/repo",
        "dani doctor",
        "dani serve --data-dir ~/.dani",
        "GitHub webhook",
    ]:
        assert expected in readme


def test_security_policy_exists_and_mentions_private_reporting() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "experimental" in security.lower()
    assert "GitHub Security Advisory" in security
    assert "Do not put secrets" in security


def test_changelog_has_unreleased_and_initial_experimental_sections() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "## Unreleased" in changelog
    assert "## 0.0.1" in changelog
    assert "Experimental public release" in changelog

"""Documentation checks for the F1 SETUP LAB restoration (Requirement 10).

Substring and word-count checks on f1-setup-lab/README.md, the root README.md and the
scripts/check-release.sh header comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
F1_README = ROOT / "f1-setup-lab" / "README.md"
ROOT_README = ROOT / "README.md"
CHECK_RELEASE = ROOT / "scripts" / "check-release.sh"

WORKSPACES = ["Setup Engineering", "Historical F1"]

# Names of the removed recorded-stint feature. They are written in pieces so that a repository-wide search for them finds
# only the migration that removes its data (db/migrations/game_data.py) and not this check.
REMOVED_FEATURE = [
    "EA SP" + "ORTS", "F1 " + "25", "U" + "DP", "20" + "777", "Live " + "Simulator", "live_" + "simulator", "SIM-" + "RECORDED",
    "SIMULATOR-" + "RECORDED", "probe_" + "setup", "verify_setup_" + "mapping", "inspect_u" + "dp_recording", "recorded " + "packets",
]
FORBIDDEN = [
    "simulator was removed",
    "simulator has been removed",
    "removed the simulator",
    "simulator is removed",
    "does not use the sdk",
    "doesn't use the sdk",
    "does not use the trialforge",
    "not used by f1",
    "not used by the f1",
    "without the sdk",
]


def _plain(text: str) -> str:
    """Strip markdown emphasis/code marks and collapse whitespace."""
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _section(text: str, heading: str, level: int) -> str:
    """Body of a markdown section: from its heading to the next heading of the same or higher level."""
    marker = "#" * level
    match = re.search(rf"^{marker} {re.escape(heading)}\s*$", text, re.MULTILINE)
    assert match, f"section '{heading}' not found"
    rest = text[match.end():]
    end = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _words(text: str) -> int:
    return len(_plain(text).split())


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", _plain(text)) if s]


def _header(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#!"):
            continue
        if not line.startswith("#"):
            break
        lines.append(line.lstrip("#"))
    return _plain(" ".join(lines))


@pytest.fixture(scope="module")
def f1() -> str:
    return F1_README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def root() -> str:
    return ROOT_README.read_text(encoding="utf-8")


# Requirement 10.1
@pytest.mark.parametrize("name", WORKSPACES)
def test_workspace_section_present_and_short(f1: str, name: str) -> None:
    body = _section(f1, name, 3)
    assert 0 < _words(body) <= 100


# Requirement 10.2
def test_sdk_named_with_role_in_at_most_three_sentences(f1: str) -> None:
    body = _section(f1, "SDK dependency", 2)
    assert "TRIALFORGE SDK" in _plain(body)
    assert 1 <= len(_sentences(body)) <= 3


# Requirement 10.3
def test_synthetic_statements_and_two_data_modes(f1: str) -> None:
    text = _plain(f1)
    assert "SYNTHETIC" in text
    assert "labelled SYNTHETIC" in text
    assert "two separate workspaces" in text
    assert "Historical data is real OpenF1 data" in text


# Requirement 10.4
def test_root_readme_describes_f1_as_using_sdk(root: str) -> None:
    rows = [ln for ln in root.splitlines() if "F1 SETUP LAB" in ln and ln.startswith("|")]
    assert rows, "F1 SETUP LAB table row missing"
    assert "built on the SDK" in _plain(rows[0])
    text = _plain(root)
    assert "TRIALFORGE" in text
    assert "both apps use the SDK" in text


# Requirement 10.5
@pytest.mark.parametrize("phrase", FORBIDDEN)
def test_root_readme_forbidden_phrases_absent(root: str, phrase: str) -> None:
    assert phrase not in _plain(root).lower()


# Requirement 10.6
@pytest.mark.parametrize("readme", ["f1", "root"])
def test_fastf1_not_supported_and_openf1_history(request: pytest.FixtureRequest, readme: str) -> None:
    text = _plain(request.getfixturevalue(readme))
    assert "FastF1 is not supported" in text
    assert re.search(r"historical data comes from OpenF1", text)


# Requirement 10.7 (replaced): the removed recorded-stint feature is not described as current anywhere in the READMEs
@pytest.mark.parametrize("readme", ["f1", "root"])
def test_removed_feature_is_not_mentioned(request: pytest.FixtureRequest, readme: str) -> None:
    text = _plain(request.getfixturevalue(readme))
    for name in REMOVED_FEATURE:
        assert name.lower() not in text.lower(), name
    assert "not been tested against the live" not in text


# Requirement 10.8
def test_check_release_header_states_both_apps_use_sdk() -> None:
    header = _header(CHECK_RELEASE)
    assert "Both applications" in header
    assert "WATTWISE LAB" in header and "F1 SETUP LAB" in header
    assert "use the TRIALFORGE_SDK" in header
    for phrase in FORBIDDEN:
        assert phrase not in header.lower()


# Requirement 10.5 / 10.9: forbidden phrases also absent from the F1 README
@pytest.mark.parametrize("phrase", FORBIDDEN)
def test_f1_readme_forbidden_phrases_absent(f1: str, phrase: str) -> None:
    assert phrase not in _plain(f1).lower()

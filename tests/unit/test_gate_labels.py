"""The V2 label templates + the V6 expert protocol artifacts are well-formed.

These are human-filled templates (Trial V2 gold labels, Trial V6 expert answers), not code, but a
broken template wastes annotator time and a drifted expert template would not score under the
shared contract. This keeps them honest: every label template parses as TOML and exposes its
documented row array, and the expert answers template carries the contract field names so the V3
harness can read it. Model-free, DB-free.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent.parent
LABELS_DIR = _ROOT / "tests" / "fixtures" / "gate_corpus" / "labels"
TRIALS_DIR = _ROOT / "docs" / "trials"

# Each V2 label family: its template file and the TOML array-of-tables key its rows live under.
LABEL_FAMILIES = {
    "gold_edges.toml": "edge",
    "gold_hypothesis_states.toml": "state",
    "gold_faithfulness.toml": "span",
    "gold_entity_clusters.toml": "mention",
    "gold_levels.toml": "level",
}

# The shared BaselineAnswer contract fields the expert answers template must carry (mirrored from
# iknos.baselines.contract; named here as strings so this test stays off that module).
CONTRACT_FIELDS = {"question_id", "answer_text", "cited_chunk_ids", "confidence"}


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("filename", "row_key"), LABEL_FAMILIES.items())
def test_label_template_parses_and_has_its_row_array(filename: str, row_key: str) -> None:
    data = _load(LABELS_DIR / filename)
    rows = data.get(row_key)
    assert isinstance(rows, list) and rows, f"{filename} has no [[{row_key}]] rows"


def test_levels_template_is_per_annotator() -> None:
    # gold_levels is the agreement-gated family: rows carry an `annotator` tag so the two
    # independent passes can be compared (Cohen's kappa).
    rows = _load(LABELS_DIR / "gold_levels.toml")["level"]
    assert all("annotator" in r for r in rows)
    assert {r["annotator"] for r in rows} == {"A", "B"}


def test_instructions_exists_and_is_jargon_free_safe() -> None:
    text = (LABELS_DIR / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert text.strip()
    # The golden rule (do not read the answer key) must be stated.
    assert "README.md" in text and "manifest.toml" in text
    # A worked example for each family (INSTRUCTIONS.md promises two each).
    assert text.count("Worked example") >= 10


def test_expert_protocol_and_template_exist() -> None:
    assert (TRIALS_DIR / "e1_expert_search_protocol.md").read_text(encoding="utf-8").strip()
    assert (TRIALS_DIR / "e1_expert_answers_template.toml").read_text(encoding="utf-8").strip()


def test_expert_answers_template_matches_contract() -> None:
    data = _load(TRIALS_DIR / "e1_expert_answers_template.toml")
    answers = data.get("answers")
    assert isinstance(answers, list) and answers, "expert template has no [[answers]] rows"
    # The example row carries every shared-contract field, so a filled file scores under V3.
    assert set(answers[0]) >= CONTRACT_FIELDS, "expert template row is missing contract fields"
    assert "meta" in data and data["meta"].get("baseline") == "expert_search"

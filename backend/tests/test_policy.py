import pytest

from app.policy.clauses import PolicyParseError, load_policy
from tests.conftest import REAL_DATA

POLICY = REAL_DATA / "procurement_policy.md"


@pytest.fixture(scope="module")
def doc():
    return load_policy(POLICY)


def test_metadata(doc):
    assert doc.doc_label == "FIN-POL-014 v3.2"
    assert doc.metadata["Effective"] == "01-Jan-2025"


def test_expected_clause_ids(doc):
    ids = [c.id for c in doc.clauses]
    assert ids == ["1", "2.1", "2.2", "2.3", "3.T", "3.T1", "3.T2", "3.T3", "3.T4", "3.1",
                   "4.1", "4.2", "4.3", "5.1", "5.2", "6.1", "7.1", "7.2", "8.1", "8.2", "9.1"]


def test_clauses_are_verbatim(doc):
    src = POLICY.read_text()
    for c in doc.clauses:
        assert src[c.start:c.end] == c.text
        assert c.text in src


def test_table_rows(doc):
    assert doc.get("3.T3").text == "| 50,000.01 – 250,000 | Finance Director |"
    assert doc.get("3.T3").table_header == "| Invoice / PO Value (EUR) | Required Approver |"
    assert doc.get("3.T3").section_title == "Approval Thresholds"


def test_ecb_clause_present(doc):
    assert "ECB reference rate" in doc.get("5.1").text


def test_quote_verification(doc):
    assert doc.quote_in_clause("3.T3", "50,000.01 – 250,000 | Finance Director")
    assert doc.quote_in_clause("5.1", "The Group reporting currency is EUR.")  # bold markers ignored
    assert doc.quote_in_clause("7.2", "Self-approval   is prohibited")        # whitespace ignored
    assert not doc.quote_in_clause("7.2", "Self-approval is allowed")
    assert not doc.quote_in_clause("99.9", "anything")
    assert not doc.quote_in_clause("7.2", "  ")


def test_policy_file_unchanged_by_loading(doc):
    import hashlib
    assert hashlib.sha256(POLICY.read_bytes()).hexdigest() == doc.sha256


def test_missing_policy(tmp_path):
    with pytest.raises(PolicyParseError):
        load_policy(tmp_path / "nope.md")

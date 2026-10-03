from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

reputation = importlib.import_module("ai_studio.reputation_manager")


def test_reputation_db_path_override_and_crud(monkeypatch, tmp_path) -> None:
    override = tmp_path / "tenant-reputation.sqlite3"
    original_path = reputation.DB_PATH
    monkeypatch.setenv("LOTVUE_REPUTATION_DB", str(override))
    try:
        importlib.reload(reputation)
        assert reputation.DB_PATH == override.resolve()

        added = reputation.add_review("google", "Buyer", 2, "Slow, disappointing service")
        review_id = added["id"]
        assert added["flagged"] is True
        assert reputation.get_reviews(limit=5)[0]["id"] == review_id
        assert reputation.respond_to_review(review_id, "We are sorry; please contact our team.")["status"] == "ok"
        assert reputation.respond_to_review(999999, "No such review")["error"] == "Review not found"
        assert reputation.get_reputation_dashboard()["total_reviews"] == 1
        assert override.is_file()
    finally:
        reputation.DB_PATH = original_path

import json
from pathlib import Path
import unittest

from evals.select_tasks import (
    CANONICAL_MBPP_PLUS_V0_2_0_IDS,
    DEFAULT_EXCLUDED,
    select_tasks,
)


class SelectTasksTests(unittest.TestCase):
    def test_output_equals_task_ids_selected_in_pilot_manifest(self):
        """Assert output equals task_ids_selected in evals/manifests/pilot.json."""
        repo_root = Path(__file__).resolve().parents[2]
        manifest_path = repo_root / "evals" / "manifests" / "pilot.json"
        self.assertTrue(manifest_path.exists(), f"Manifest file missing at {manifest_path}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        frozen_ids = manifest.get("task_ids_selected")
        self.assertIsInstance(frozen_ids, list)
        self.assertEqual(len(frozen_ids), 10)

        selected = select_tasks(seed=42, count=10)
        self.assertEqual(selected, frozen_ids)

    def test_deterministic_reproduction(self):
        """Assert same seed produces identical selection across calls."""
        res1 = select_tasks(seed=42, count=10)
        res2 = select_tasks(seed=42, count=10)
        self.assertEqual(res1, res2)
        expected = [
            "Mbpp/56",
            "Mbpp/102",
            "Mbpp/108",
            "Mbpp/129",
            "Mbpp/252",
            "Mbpp/266",
            "Mbpp/282",
            "Mbpp/616",
            "Mbpp/752",
            "Mbpp/775",
        ]
        self.assertEqual(res1, expected)

    def test_smoke_tasks_excluded(self):
        """Assert default excluded smoke tasks are never selected."""
        selected = select_tasks(seed=42, count=10)
        for excluded in DEFAULT_EXCLUDED:
            self.assertNotIn(excluded, selected)

    def test_canonical_integer_sort_order(self):
        """Assert results are sorted canonically by integer ID."""
        selected = select_tasks(seed=42, count=10)
        int_ids = [int(x.split("/")[-1]) for x in selected]
        self.assertEqual(int_ids, sorted(int_ids))


if __name__ == "__main__":
    unittest.main()

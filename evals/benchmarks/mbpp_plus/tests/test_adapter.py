import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapter import read_jsonl, select_ids, validate_samples, write_jsonl


class SampleValidationTests(unittest.TestCase):
    def test_accepts_exactly_one_solution_per_task(self):
        validate_samples(["2", "3"], [
            {"task_id": "2", "solution": "def f(): return 1"},
            {"task_id": "3", "solution": "def g(): return 2"},
        ])

    def test_rejects_missing_and_extra_tasks(self):
        with self.assertRaisesRegex(ValueError, "mismatch"):
            validate_samples(["2", "3"], [{"task_id": "2", "solution": "def f(): return 1"}])
        with self.assertRaisesRegex(ValueError, "mismatch"):
            validate_samples(["2"], [
                {"task_id": "2", "solution": "def f(): return 1"},
                {"task_id": "3", "solution": "def g(): return 2"},
            ])

    def test_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_samples(["2"], [
                {"task_id": "2", "solution": "def f(): return 1"},
                {"task_id": "2", "solution": "def f(): return 2"},
            ])

    def test_rejects_empty_solution(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            validate_samples(["2"], [{"task_id": "2", "solution": " "}])

    def test_select_ids_sorts_canonical_mbpp_ids_numerically(self):
        tasks = {"Mbpp/10": {}, "Mbpp/2": {}, "Mbpp/3": {}}
        self.assertEqual(select_ids(tasks, None, 2), ["Mbpp/2", "Mbpp/3"])
        self.assertEqual(select_ids(tasks, ["Mbpp/10", "Mbpp/2"], 2), ["Mbpp/10", "Mbpp/2"])

    def test_jsonl_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            rows = [{"task_id": "2", "prompt": "first line\nsecond line"}]
            write_jsonl(path, rows)
            self.assertEqual(read_jsonl(path), rows)


if __name__ == "__main__":
    unittest.main()

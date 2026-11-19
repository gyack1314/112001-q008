import json
from pathlib import Path
import unittest


class InputShapeTest(unittest.TestCase):
    def test_records_declare_fact_kind(self):
        rows = json.loads((Path(__file__).parents[1] / "data" / "facts.json").read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(all(row["fact_kind"] for row in rows))


if __name__ == "__main__":
    unittest.main()

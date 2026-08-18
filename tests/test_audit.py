import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


AUDIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit.py"
SPEC = importlib.util.spec_from_file_location("ocsr_audit", AUDIT_PATH)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)


class StructureLinksTest(unittest.TestCase):
    def test_structure_links_are_relative_to_structure_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            (docs / "problems" / "bugfix").mkdir(parents=True)
            (docs / "CURRENT.md").write_text("# Current\n", encoding="utf-8")
            (docs / "problems" / "bugfix" / "fixed.md").write_text(
                "# Fixed\n", encoding="utf-8"
            )
            (docs / "STRUCTURE.md").write_text(
                "# Index\n\n"
                "| Topic | Document |\n"
                "|---|---|\n"
                "| Current | [CURRENT](CURRENT.md) |\n"
                "| Bugs | [bugs](problems/bugfix/) |\n",
                encoding="utf-8",
            )

            with patch.object(audit, "ROOT", root):
                results = audit._check_structure()

            failures = [r for r in results if r["status"] != "ok"]
            self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from investment_tracker.workspace import WorkspaceError, WorkspacePaths


class WorkspacePathsTests(unittest.TestCase):
    def test_requires_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"

            with self.assertRaisesRegex(WorkspaceError, "does not exist"):
                WorkspacePaths.from_path(missing)

    def test_rejects_required_input_symlink_outside_workspace(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            outside_file = Path(outside) / "brokerage-current.json"
            outside_file.write_text("{}\n", encoding="utf-8")
            (root / "brokerage-current.json").symlink_to(outside_file)
            workspace = WorkspacePaths.from_path(root)

            with self.assertRaisesRegex(WorkspaceError, "escapes workspace"):
                workspace.require_input(workspace.brokerage_current)

    def test_exposes_fixed_workspace_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = WorkspacePaths.from_path(root)
            resolved = root.resolve()

            self.assertEqual(workspace.brokerage_current, resolved / "brokerage-current.json")
            self.assertEqual(workspace.brokerage_ledger, resolved / "brokerage-ledger.jsonl")
            self.assertEqual(workspace.market_manifest, resolved / "data/market/manifest.json")
            self.assertEqual(workspace.market_data, resolved / "data/market")
            self.assertEqual(workspace.reports, resolved / "reports")


if __name__ == "__main__":
    unittest.main()

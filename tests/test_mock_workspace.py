import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures/mock-workspace"


class MockWorkspaceTests(unittest.TestCase):
    def test_offline_workspace_builds_and_exports_both_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            shutil.copytree(FIXTURE, workspace)

            for command in (
                "build",
                "check",
                "export-chatgpt",
                "check-market-analysis",
                "export-market-analysis",
            ):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "investment_tracker",
                        "--workspace",
                        str(workspace),
                        command,
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            with zipfile.ZipFile(workspace / "reports/chatgpt-export.zip") as archive:
                self.assertIn("chatgpt-export/performance.md", archive.namelist())
            with zipfile.ZipFile(workspace / "reports/market-analysis.zip") as archive:
                self.assertIn("market-analysis/analysis.md", archive.namelist())


if __name__ == "__main__":
    unittest.main()

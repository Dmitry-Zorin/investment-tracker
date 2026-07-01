import unittest

from investment_tracker.cli import build_parser


class CliContractTests(unittest.TestCase):
    def test_workspace_is_required(self):
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["check"])

    def test_market_analysis_commands_share_main_cli(self):
        args = build_parser().parse_args(
            ["--workspace", "/tmp/example", "export-market-analysis"]
        )

        self.assertEqual(args.command, "export-market-analysis")


if __name__ == "__main__":
    unittest.main()

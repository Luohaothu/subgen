import unittest
from unittest.mock import patch

from subtitle_pipeline import cli


class CliTests(unittest.TestCase):
    def test_default_runs_the_pipeline(self):
        with (
            patch("subtitle_pipeline.cli.runner_cli.main", return_value=0) as runner,
            patch("subtitle_pipeline.cli.serve_cli.main") as serve,
        ):
            self.assertEqual(cli.main(["--dry-run"]), 0)
            runner.assert_called_once_with(["--dry-run"])
            serve.assert_not_called()

    def test_serve_dispatches_to_the_server(self):
        with (
            patch("subtitle_pipeline.cli.runner_cli.main") as runner,
            patch("subtitle_pipeline.cli.serve_cli.main", return_value=3) as serve,
        ):
            self.assertEqual(cli.main(["serve", "--model-path", "model"]), 3)
            serve.assert_called_once_with(["--model-path", "model"])
            runner.assert_not_called()

    def test_no_arguments_runs_the_pipeline(self):
        with patch("subtitle_pipeline.cli.runner_cli.main", return_value=0) as runner:
            self.assertEqual(cli.main([]), 0)
            runner.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()

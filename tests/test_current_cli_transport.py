import subprocess
import unittest
from unittest.mock import patch

from utmail_tool.vimbrowser import run_vimbrowser
from utmail_tool.errors import BrowserImportError


class CurrentCliTransportTests(unittest.TestCase):
    @patch("utmail_tool.vimbrowser.subprocess.run")
    def test_frame_tree_uses_native_cli(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
        run_vimbrowser(["vimbrowser-cli", "frame-tree", "89"], input=None, capture_output=True)
        self.assertEqual(run.call_args.args[0], ["vimbrowser-cli", "frame-tree", "89"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    @patch("utmail_tool.vimbrowser.subprocess.run")
    def test_frame_script_preserves_exact_utf8(self, run):
        source = "'a\\nb'; // λ\n"
        run_vimbrowser(["vimbrowser-cli", "frame-js", "89", "frame"], input=source, capture_output=True)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["vimbrowser-cli", "frame-js", "89", "frame"])
        self.assertEqual(run.call_args.kwargs["input"], source)

    @patch("utmail_tool.vimbrowser.subprocess.run")
    def test_missing_script_does_not_run(self, run):
        with self.assertRaises(BrowserImportError):
            run_vimbrowser(["vimbrowser-cli", "frame-js", "89", "frame"], input=None)
        run.assert_not_called()

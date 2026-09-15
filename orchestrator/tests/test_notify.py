import os
import tempfile
import unittest
from pathlib import Path

from core.notify import NotifyError, notify


class NotifyTests(unittest.TestCase):
    def test_ignores_non_terminal_status(self):
        self.assertFalse(notify("WAITING", "job1", script="/missing"))

    def test_calls_existing_script(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / "args.txt"
            script = td / "notify.sh"
            script.write_text(f'#!/bin/sh\nprintf "%s\\n%s\\n%s\\n" "$1" "$2" "$3" > "{out}"\n')
            os.chmod(script, 0o755)
            self.assertTrue(notify("DONE", "job1", "all good", script=script))
            self.assertEqual(out.read_text().splitlines(), ["DONE", "job1", "all good"])

    def test_script_failure_raises(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "bad.sh"
            script.write_text('#!/bin/sh\necho boom >&2\nexit 7\n')
            os.chmod(script, 0o755)
            with self.assertRaisesRegex(NotifyError, "exited 7"):
                notify("ERROR", "job1", script=script)

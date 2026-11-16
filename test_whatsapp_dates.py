from contextlib import closing, nullcontext, redirect_stderr, redirect_stdout
import csv
import io
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import whatsapp_dates as app


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.folder / "msgstore.db"
        self.journal = self.folder / "test.sqlite3"
        self.adb = self.folder / "adb.exe"
        self.adb.touch()
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                CREATE TABLE message (_id INTEGER PRIMARY KEY, timestamp INTEGER, body TEXT);
                CREATE TABLE message_media (message_row_id, file_path, media_name, mime_type);
                INSERT INTO message VALUES (1, 1705543248123, 'private text'), (2, 1705543249123, NULL);
                INSERT INTO message_media VALUES
                    (1, 'WhatsApp Images/VISIBLE.JPG', NULL, 'image/jpeg'),
                    (1, NULL, 'hidden.jpeg', 'image/jpeg'),
                    (1, NULL, 'dup.jpg', 'image/jpeg'),
                    (1, NULL, 'conflict.jpg', 'image/jpeg'),
                    (2, NULL, 'CONFLICT.JPG', 'image/jpeg'),
                    (1, NULL, 'absent.jpg', 'image/jpeg');
            """)
        self.visible = str(app.BASE / "visible.jpg")
        self.hidden = str(app.BASE / "Private" / "hidden.jpeg")
        self.original = 1735689600123456789
        self.files = dict.fromkeys([
            self.visible, self.hidden, str(app.BASE / "dup.jpg"), str(app.BASE / "Sent" / "DUP.JPG"),
            str(app.BASE / "conflict.jpg"), str(app.BASE / "extra.jpg"),
        ], self.original)
        self.touched = []
        self.scanned = []
        self.restarts = 0
        self.fail_scan = False
        self.enterContext(patch.object(app.subprocess, "run", side_effect=self.mock_adb))
        self.enterContext(patch.object(app.Adb, "media_session", return_value=nullcontext()))
        self.enterContext(patch.object(app.Adb, "media_batch", side_effect=self.mock_media))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))

    def mock_adb(self, command, input=None, **_):
        def stat_line(path):
            stamp = app.format_time(self.files[path])
            return f"42 99 1500 81b0 {stamp[:10]} {stamp[11:-5]} +0000\n"

        args = command[1:]
        output = ""
        if args == ["devices"]:
            output = "List of devices attached\nserial\tdevice\n"
        elif args == ["-s", "serial", "get-state"]:
            output = "device\n"
        elif args != ["start-server"]:
            self.assertEqual(args[:4], ["-s", "serial", "shell", "-T"])
            script = args[4]
            if script == "sh":
                result = bytearray()
                for line in input.decode().splitlines()[::2]:
                    operation = line.removeprefix("output=$( ( ").removesuffix(" ) 2>&1 </dev/null); code=$?")
                    item = self.mock_adb([*command[:-1], operation])
                    result.extend(str(item.returncode).encode() + b"\0" + item.stdout.strip() + b"\0")
                return subprocess.CompletedProcess(command, 0, bytes(result), b"")
            words = shlex.split(script)
            if script.startswith("set -e;"):
                output = "\0".join(self.files) + "\0"
            elif words[:2] == ["toybox", "stat"]:
                output = "".join(stat_line(path) for path in words[4:])
            elif "touch" in words:
                path = words[words.index("-d") + 2]
                stamp = words[words.index("-d") + 1].replace("T", " ")
                self.files[path] = app.parse_stat(f"42 99 1500 81b0 {stamp[:-5]} {stamp[-5:]}").mtime
                self.touched.append(path)
                self.assertIn("&& toybox stat", script)
                output = stat_line(path)
            elif words[:2] == ["am", "force-stop"]:
                self.restarts += 1
            else:
                self.fail(f"Unexpected command: {script}")
        return subprocess.CompletedProcess(command, 0, output.encode(), b"")

    def mock_media(self, commands):
        results = []
        for kind, *fields in commands:
            if kind == "scan":
                self.assertEqual(fields, [self.visible])
                self.scanned.append(self.visible)
                results.append("Result: Bundle[{android.intent.extra.STREAM=Supplier{VAL_PARCELABLE@64+136}}]")
            else:
                expected = self.files[self.visible] // app.SECOND
                results.append(f"Row: 0 _display_name=visible.jpg, date_modified={expected - self.fail_scan}\r\n"
                               "Row: 1 _display_name=other.jpg, date_modified=0")
        return results

    def cli(self, *args):
        return app.main(["--adb", str(self.adb), "--db", str(self.db),
                         "--journal", str(self.journal), *args])

    def test_preview_is_read_only_and_excludes_conflicts(self):
        report = self.folder / "preview.csv"
        before = self.db.read_bytes()
        self.assertEqual(self.cli("--report", str(report)), 0)
        with report.open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertCountEqual([row["status"] for row in rows], [
            "needs_change", "needs_change", "ambiguous", "ambiguous", "conflict",
            "unmatched_device", "unmatched_database",
        ])
        self.assertNotIn("private text", report.read_text(encoding="utf-8-sig"))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.touched, [])

    def test_apply_limit_resume_and_rollback(self):
        self.fail_scan = True
        self.assertEqual(self.cli("--apply", "--limit", "1"), 1)
        self.assertEqual(self.touched, [self.visible])
        self.assertEqual(self.files[self.visible], 1705543248123000000)
        self.fail_scan = False
        self.assertEqual(self.cli("--apply"), 0)
        self.assertEqual(self.touched, [self.visible, self.hidden])
        self.assertEqual(self.scanned, [self.visible, self.visible])
        self.assertEqual(self.cli("--apply"), 0)
        self.assertEqual(self.restarts, 2)
        self.assertEqual(self.cli("--rollback"), 0)
        self.assertEqual(set(self.files.values()), {self.original})
        self.assertEqual(self.scanned, [self.visible] * 3)
        self.assertEqual(self.restarts, 3)

    def test_rollback_rejects_changed_file_and_continues(self):
        self.assertEqual(self.cli("--apply"), 0)
        self.files[self.visible] += 1
        self.assertEqual(self.cli("--rollback"), 1)
        self.assertEqual(self.files[self.visible], 1705543248123000001)
        self.assertEqual(self.files[self.hidden], self.original)


if __name__ == "__main__":
    unittest.main()

from contextlib import chdir, closing, nullcontext, redirect_stderr, redirect_stdout
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
        self.video = str(app.VIDEO_BASE / "clip.mp4")
        self.original = 1735689600123456789
        self.files = dict.fromkeys([
            self.visible, self.hidden, str(app.BASE / "dup.jpg"), str(app.BASE / "Sent" / "DUP.JPG"),
            str(app.BASE / "conflict.jpg"), str(app.BASE / "extra.jpg"),
        ], self.original)
        self.touched = []
        self.scanned = []
        self.restarts = 0
        self.fail_scan = False
        self.album = app.REVIEW_ROOT / (app.REVIEW_PREFIX + "test1234")
        self.digests = {}
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
            elif "sha256sum" in words:
                path = words[4]
                output = stat_line(path) + f"{self.digests.get(path, '00' * 32)}  -\n" + stat_line(path)
            elif "cp" in words:
                offset = words.index("cp")
                source, destination = words[offset + 3:offset + 5]
                self.files[destination] = self.files[source]
                output = stat_line(destination)
            elif "mv" in words:
                offset = words.index("mv")
                source, destination = words[offset + 2:offset + 4]
                manifest = Path(f"{self.album.name}-movimentos.csv")
                with manifest.open(encoding="utf-8-sig", newline="") as handle:
                    self.assertIn([source, destination], list(csv.reader(handle)))
                self.files[destination] = self.files.pop(source)
                output = stat_line(destination)
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
            elif words[0] == "mktemp":
                output = str(self.album)
            elif words[0] == "mkdir":
                self.assertEqual(words[-1], str(app.REVIEW_ROOT))
            else:
                self.fail(f"Unexpected command: {script}")
        return subprocess.CompletedProcess(command, 0, output.encode(), b"")

    def mock_media(self, commands):
        results = []
        for kind, *fields in commands:
            if kind == "scan":
                self.assertTrue(fields[0] in (self.visible, self.video) or app.PurePosixPath(fields[0]).parent == self.album)
                self.scanned.append(fields[0])
                results.append("Result: Bundle[{android.intent.extra.STREAM=Supplier{VAL_PARCELABLE@64+136}}]")
            else:
                media, _, where = fields
                if self.album.name in where:
                    paths = [path for path in self.files if app.PurePosixPath(path).parent == self.album
                             and f"'{Path(path).name}'" in where]
                else:
                    paths = [{"images": self.visible, "videos": self.video}[media]]
                rows = [f"Row: {index} _display_name={Path(path).name}, "
                        f"date_modified={self.files[path] // app.SECOND - self.fail_scan}"
                        for index, path in enumerate(paths)]
                results.append("\r\n".join(rows + ["Row: 9 _display_name=other.jpg, date_modified=0"]))
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

    def test_video_scope_and_rollback_preserve_photo_changes(self):
        hidden_video = str(app.VIDEO_BASE / "Private" / "clip-hidden.mov")
        self.files.update({self.video: self.original, hidden_video: self.original})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executemany("INSERT INTO message_media VALUES (1, NULL, ?, ?)", [
                ("CLIP.MP4", "video/mp4"), ("clip-hidden.mov", "video/quicktime"),
            ])
        self.assertEqual(self.cli("--apply", "--media", "images", "--limit", "1"), 0)
        adjusted = self.files[self.visible]
        self.assertEqual(self.cli("--apply", "--media", "videos"), 0)
        self.assertEqual(self.touched, [self.visible, self.video, hidden_video])
        self.assertEqual(self.files[self.video], adjusted)
        self.assertEqual(self.files[hidden_video], adjusted)
        self.assertEqual(self.cli("--rollback", "--media", "videos"), 0)
        self.assertEqual(self.files[self.visible], adjusted)
        self.assertEqual(self.files[self.video], self.original)
        self.assertEqual(self.files[hidden_video], self.original)
        self.assertEqual(self.scanned, [self.visible, self.video, self.video])
        self.assertEqual(self.cli("--apply"), 0)
        self.assertEqual(self.files[self.hidden], adjusted)
        self.assertEqual(self.files[self.video], adjusted)
        self.assertEqual(self.files[hidden_video], adjusted)

    def test_hash_recovers_exact_content_and_album_keeps_originals(self):
        extra = str(app.BASE / "extra.jpg")
        conflict = str(app.BASE / "conflict.jpg")
        hidden = str(app.BASE / "Sent" / "DUP.JPG")
        self.digests.update({extra: "aa" * 32, conflict: "bb" * 32})
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("ALTER TABLE message_media ADD COLUMN file_hash TEXT")
            db.execute("UPDATE message_media SET file_hash = ? WHERE lower(media_name) = 'conflict.jpg'", ("bb" * 32,))
            db.execute("INSERT INTO message_media VALUES (1, NULL, 'previous-name.jpg', NULL, ?)", ("aa" * 32,))
        report = self.folder / "hash.csv"
        self.assertEqual(self.cli("--report", str(report)), 0)
        with report.open(encoding="utf-8-sig", newline="") as source:
            statuses = {row["device_path"]: row for row in csv.DictReader(source)}
        self.assertEqual(statuses[extra]["status"], "needs_change")
        self.assertEqual(statuses[conflict]["status"], "conflict")
        self.assertIn(" | ", statuses[conflict]["candidate_message_utc"])
        originals = self.files.copy()
        self.assertEqual(self.cli("--review-album"), 0)
        self.assertEqual({path: self.files[path] for path in originals}, originals)
        self.assertIn(str(self.album / "conflict.jpg"), self.files)
        self.assertNotIn(str(self.album / "extra.jpg"), self.files)
        self.assertNotIn(str(self.album / app.PurePosixPath(hidden).name), self.files)
        self.album = app.REVIEW_ROOT / (app.REVIEW_PREFIX + "move1234")
        with chdir(self.folder):
            self.assertEqual(self.cli("--review-album", "--move", "--media", "images"), 0)
        self.assertNotIn(conflict, self.files)
        self.assertEqual(self.files[str(self.album / "conflict.jpg")], originals[conflict])
        self.assertEqual(self.files[hidden], originals[hidden])


if __name__ == "__main__":
    unittest.main()

"""Restore WhatsApp JPEG modification times using a decrypted message database."""

import argparse
from base64 import b64encode
from collections import Counter, defaultdict
from contextlib import closing, contextmanager, nullcontext
import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from math import ceil
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
from time import monotonic
from typing import NamedTuple


BASE = PurePosixPath("/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images")
DIRECTORIES = (BASE, BASE / "Private", BASE / "Sent")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SECOND = 1_000_000_000
STAT_FORMAT = "%d %i %s %f %y"
BATCH_SIZE = 60
MEDIA_DEX = Path(__file__).resolve().parent / "android" / "whatsapp-dates-media.dex"
STAT_LINE = re.compile(
    r"(\d+) (\d+) (\d+) ([0-9a-fA-F]+) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
    r"(?:\.(\d{1,9}))? ([+-]\d{2}:?\d{2}|Z)"
)
JOURNAL_ID = 0x57414431
ERRORS = (OSError, RuntimeError, ValueError, OverflowError, sqlite3.Error, subprocess.SubprocessError)


class FileInfo(NamedTuple):
    identity: str
    mtime: int


class Match(NamedTuple):
    filename: str
    path: str
    status: str
    current: int | None = None
    target: int | None = None
    identity: str | None = None


class Progress:
    def __init__(self, label, total):
        self.label = label
        self.total = total
        self.start = monotonic()
        self.last = self.start
        self.terminal = sys.stderr.isatty()
        self.width = 0
        self.has_estimate = False
        if total:
            self.update(0)

    def update(self, done):
        now = monotonic()
        complete = done == self.total
        if self.has_estimate and not complete and now - self.last < (1 if self.terminal else 15):
            return
        ratio = done / self.total
        filled = int(ratio * 20)
        remaining = str(timedelta(seconds=ceil((now - self.start) * (self.total - done) / done))) if done else "--:--:--"
        line = (f"{self.label} [{'#' * filled}{'-' * (20 - filled)}] {ratio:6.1%}"
                f"  {done}/{self.total}  |  faltam ~{remaining}")
        print(line.ljust(self.width), end="\r" if self.terminal and not complete else "\n",
              file=sys.stderr, flush=True)
        self.width = len(line) if self.terminal and not complete else 0
        self.last = now
        self.has_estimate = done > 0

    def end_line(self):
        if self.width:
            print(file=sys.stderr, flush=True)
            self.width = 0


def stat_batches(candidates):
    batch = []
    size = 0
    for row in candidates:
        length = len(shlex.quote(row[1]).encode("utf-8")) + 1
        if batch and size + length > 12_000:
            yield batch
            batch = []
            size = 0
        batch.append(row)
        size += length
    if batch:
        yield batch


def format_time(nanoseconds):
    seconds, fraction = divmod(nanoseconds, SECOND)
    date = EPOCH + timedelta(seconds=seconds)
    return f"{date:%Y-%m-%dT%H:%M:%S}.{fraction:09d}+0000"


def parse_stat(line):
    match = STAT_LINE.fullmatch(line)
    if match is None:
        raise ValueError(f"Resposta inesperada de stat: {line!r}")
    device, inode, size, mode, date, fraction, zone = match.groups()
    if not stat.S_ISREG(int(mode, 16)):
        raise ValueError("O caminho deixou de apontar para um arquivo regular.")
    delta = datetime.fromisoformat(f"{date}{zone}") - EPOCH
    seconds = delta.days * 86400 + delta.seconds
    return FileInfo(f"{device}:{inode}:{size}", seconds * SECOND + int((fraction or "").ljust(9, "0")))


def jpeg_name(value):
    if isinstance(value, str):
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        if name.lower().endswith((".jpg", ".jpeg")):
            return name.casefold()
    return None


def allowed_path(path):
    candidate = PurePosixPath(path)
    return (
        str(candidate) == path
        and candidate.parent in DIRECTORIES
        and "\\" not in candidate.name
        and candidate.name.lower().endswith((".jpg", ".jpeg"))
    )


def scan_candidate(path):
    file = PurePosixPath(path)
    return file.parent == BASE and not file.name.startswith(".")


def read_dates(database):
    dates = defaultdict(set)
    query = """
        SELECT mm.file_path, mm.media_name, m.timestamp
        FROM message_media mm JOIN message m ON m._id = mm.message_row_id
        WHERE mm.mime_type LIKE 'image/%'
    """
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for path, name, milliseconds in connection.execute(query):
            filename = jpeg_name(path) or jpeg_name(name)
            if filename:
                valid = isinstance(milliseconds, int) and 0 < milliseconds <= (2**63 - 1) // 1_000_000
                dates[filename].add(milliseconds * 1_000_000 if valid else None)
    return dict(dates)


class Adb:
    def __init__(self, executable, serial):
        self.executable = str(Path(executable).resolve(strict=True))
        self.run("start-server")
        if serial is None:
            devices = [line.split() for line in self.run("devices").splitlines()[1:] if line.strip()]
            authorized = [fields[0] for fields in devices if fields[1:2] == ["device"]]
            if not devices:
                raise RuntimeError("ADB não detectou nenhum aparelho. Confira USB debugging no celular e execute adb devices -l.")
            if not authorized:
                states = ", ".join(sorted({fields[1] for fields in devices if len(fields) > 1}))
                raise RuntimeError(f"ADB detectou o aparelho ({states or 'estado desconhecido'}). Desbloqueie-o, aceite Allow USB debugging e reconecte se necessário.")
            if len(authorized) > 1:
                raise RuntimeError("Há mais de um aparelho autorizado. Selecione um com --serial.")
            serial = authorized[0]
        self.serial = serial
        if self.run("-s", serial, "get-state").strip() != "device":
            raise RuntimeError("O aparelho ADB não está autorizado ou conectado.")

    def run(self, *args, data=None):
        result = subprocess.run(
            [self.executable, *args], input=data, capture_output=True,
            timeout=120 if data is not None else 60, check=False
        )
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        reports_progress = args == ("start-server",) or args[2:3] == ("push",)
        if result.returncode or (stderr and not reports_progress):
            raise RuntimeError(stderr or f"ADB retornou código {result.returncode}.")
        return result.stdout.decode("utf-8")

    def shell(self, *args):
        return self.script(shlex.join(args))

    def script(self, command):
        return self.run("-s", self.serial, "shell", "-T", command)

    def batch(self, commands):
        if not commands:
            return []
        # Isolate failures and preserve multiline output for each command.
        script = "".join(
            f'output=$( ( {command} ) 2>&1 </dev/null); code=$?\n'
            'printf "%s\\000%s\\000" "$code" "$output"\n'
            for command in commands
        )
        return self.parse_batch(self.run("-s", self.serial, "shell", "-T", "sh",
                                         data=script.encode("utf-8")), len(commands))

    @staticmethod
    def parse_batch(output, count):
        fields = output.split("\0")
        if len(fields) != 2 * count + 1 or fields[-1] or not all(
            code.isdecimal() for code in fields[:-1:2]
        ):
            raise RuntimeError("Resposta incompleta do ADB. Retome com o mesmo journal.")
        return [
            output if code == "0" else RuntimeError(output or f"Android retornou código {code}.")
            for code, output in zip(fields[:-1:2], fields[1::2], strict=True)
        ]

    @contextmanager
    def media_session(self):
        if not MEDIA_DEX.is_file():
            raise RuntimeError(f"Auxiliar Android ausente: {MEDIA_DEX}")
        directory = self.shell("mktemp", "-d", "/data/local/tmp/whatsapp-dates.XXXXXXXX").strip()
        if not re.fullmatch(r"/data/local/tmp/whatsapp-dates\.[A-Za-z0-9]+", directory):
            raise RuntimeError("O Android não forneceu um diretório temporário válido.")
        self.media_dex = directory + "/helper.dex"
        try:
            self.run("-s", self.serial, "push", str(MEDIA_DEX), self.media_dex)
            self.shell("chmod", "444", self.media_dex)
            check, = self.media_batch([("query", "_id", "_id = -1")])
            if not isinstance(check, str) or check.strip() != "No result found.":
                raise RuntimeError(f"O modo rápido de reindexação não iniciou: {check}")
            yield
        finally:
            try:
                self.script(f"rm -f {self.media_dex}; rmdir {directory}")
            except ERRORS as error:
                print(f"Erro ao limpar o auxiliar temporário do Android: {error}", file=sys.stderr)

    def media_batch(self, commands):
        lines = []
        for kind, *args in commands:
            encoded = [b64encode(arg.encode("utf-8")).decode("ascii") for arg in args]
            lines.append("\t".join((kind, *encoded)) + "\n")
        command = (f"CLASSPATH={self.media_dex}:/system/framework/content.jar "
                   "app_process /system/bin WhatsAppDatesMedia")
        output = self.run("-s", self.serial, "shell", "-T", command, data="".join(lines).encode("ascii"))
        return self.parse_batch(output, len(commands))

    def files(self):
        def find(directory):
            return shlex.join(("toybox", "find", str(directory), "-maxdepth", "1", "-type", "f", "-print0"))

        commands = [find(BASE)]
        for directory in DIRECTORIES[1:]:
            quoted = shlex.quote(str(directory))
            commands.append(f"if [ -d {quoted} ]; then {find(directory)}; fi")
        output = self.script("set -e; " + "; ".join(commands))
        paths = {path for path in output.split("\0") if allowed_path(path)}
        return sorted(paths, key=lambda path: (PurePosixPath(path).parent != BASE, path.casefold(), path))

    def stat_many(self, paths):
        lines = self.shell("toybox", "stat", "-c", STAT_FORMAT, *paths).splitlines()
        if len(lines) != len(paths):
            raise RuntimeError("Número inesperado de respostas de stat.")
        return [parse_stat(line) for line in lines]

    def stat(self, path):
        return self.stat_many([path])[0]

    def touch_command(self, path, timestamp, expected_stat):
        quoted = shlex.quote(path)
        touch = shlex.join(("toybox", "touch", "-m", "-c", "-h", "-d", format_time(timestamp), path))
        verify = shlex.join(("toybox", "stat", "-c", STAT_FORMAT, path))
        return (
            f'if [ "$({verify})" = {shlex.quote(expected_stat)} ]; then {touch} && {verify}; '
            'else printf "%s\\n" "O arquivo ou a data mudou antes da gravação." >&2; exit 1; fi'
        )

    @staticmethod
    def scan_needs_query(output):
        output = output.strip()
        if output == "hidden" or re.fullmatch(
            r"Result: Bundle\[\{android\.intent\.extra\.STREAM=content://media/[^\s{}]+\}\]", output
        ):
            return False
        if re.fullmatch(
            r"Result: Bundle\[\{android\.intent\.extra\.STREAM=Supplier\{VAL_PARCELABLE@\d+\+\d+\}\}\]",
            output,
        ):
            return True
        raise RuntimeError(f"MediaStore não confirmou a indexação: {output!r}")

    def scan_batch(self, files):
        directory = BASE.relative_to("/sdcard")

        def query(names, projection):
            literals = ", ".join("'" + name.replace("'", "''") + "'" for name in names)
            where = f"relative_path = '{directory}/' AND _display_name IN ({literals})"
            return ("query", projection, where)

        names = {PurePosixPath(path).name: (path, target // SECOND) for path, target in files}
        normal = [name for name in names if "\n" not in name and "\r" not in name]
        multiline = [name for name in names if name not in normal]
        commands = [("scan", path) for path, _ in files]
        commands.extend(query([name], "_id:date_modified") for name in multiline)
        if normal:
            commands.append(query(normal, "_display_name:date_modified"))
        results = self.media_batch(commands)
        scan_results, results = results[:len(files)], results[len(files):]
        confirmed = set()
        for name, result in zip(multiline, results[:len(multiline)], strict=True):
            path, seconds = names[name]
            if isinstance(result, str) and re.fullmatch(rf"Row: 0 _id=\d+, date_modified={seconds}", result.strip()):
                confirmed.add(path)
        if normal and isinstance(results[-1], str):
            indexed = defaultdict(list)
            for line in results[-1].split("\n"):
                row = re.fullmatch(r"Row: \d+ _display_name=(.+), date_modified=(-?\d+|NULL)",
                                   line.removesuffix("\r"))
                if row:
                    indexed[row[1]].append(row[2])
            for name in normal:
                path, seconds = names[name]
                if indexed[name] == [str(seconds)]:
                    confirmed.add(path)
        outcomes = []
        for (path, _), result in zip(files, scan_results, strict=True):
            try:
                if isinstance(result, RuntimeError):
                    raise result
                if self.scan_needs_query(result) and path not in confirmed:
                    raise RuntimeError("MediaStore ainda não mostra a data esperada.")
            except RuntimeError as error:
                outcomes.append(error)
            else:
                outcomes.append(None)
        return outcomes

    def restart_gallery(self):
        response = self.shell("am", "force-stop", "com.sec.android.gallery3d")
        if response.strip():
            raise RuntimeError(f"Resposta inesperada de Gallery: {response.strip()!r}")


def inventory(adb, dates):
    print("Listando JPEGs no aparelho...", file=sys.stderr, flush=True)
    files = defaultdict(list)
    for path in adb.files():
        files[jpeg_name(path)].append(path)
    matches = []
    candidates = []
    for filename, paths in files.items():
        values = dates.get(filename, set())
        if len(paths) > 1:
            status = "ambiguous"
        elif not values:
            status = "unmatched_device"
        elif len(values - {None}) > 1:
            status = "conflict"
        elif None in values:
            status = "invalid_timestamp"
        else:
            candidates.append((filename, paths[0], next(iter(values))))
            continue
        matches.extend(Match(filename, path, status) for path in paths)

    progress = Progress("Leitura das datas", len(candidates))
    completed = 0
    for chunk in stat_batches(candidates):
        try:
            infos = adb.stat_many([path for _, path, _ in chunk])
        except ERRORS:
            infos = []
            for _, path, _ in chunk:
                try:
                    infos.append(adb.stat(path))
                except ERRORS as error:
                    progress.end_line()
                    print(f"Erro em stat de {path!r}: {error}", file=sys.stderr)
                    infos.append(None)
        for (filename, path, target), info in zip(chunk, infos, strict=True):
            if info is None:
                matches.append(Match(filename, path, "stat_error", target=target))
            else:
                status = "unchanged" if info.mtime == target else "needs_change"
                matches.append(Match(filename, path, status, info.mtime, target, info.identity))
        completed += len(chunk)
        progress.update(completed)

    for filename in sorted(dates.keys() - files.keys()):
        values = dates[filename]
        target = next(iter(values)) if len(values) == 1 else None
        matches.append(Match(filename, "", "unmatched_database", target=target))
    return matches


def preview(matches, dates, report):
    counts = Counter(row.status for row in matches)
    conflicts = sum(len(values - {None}) > 1 for values in dates.values())
    matched = counts["needs_change"] + counts["unchanged"] + counts["stat_error"]
    print(f"Correspondências únicas: {matched} | Precisam de alteração: {counts['needs_change']}"
          f" | Já corretos: {counts['unchanged']} | Erros de stat: {counts['stat_error']}")
    print(f"Arquivos sem registro: {counts['unmatched_device']} | Nomes do banco ausentes no aparelho: "
          f"{counts['unmatched_database']} | Conflitos no banco: {conflicts}"
          f" | Arquivos excluídos por conflito: {counts['conflict']}"
          f" | Arquivos com nome duplicado: {counts['ambiguous']}"
          f" | Datas inválidas: {counts['invalid_timestamp']}")
    samples = [row for row in matches if row.status == "needs_change"][:5]
    if not samples:
        samples = [row for row in matches if row.status == "unchanged"][:5]
    for row in samples:
        print(f"  {row.filename!r}: {format_time(row.current)} → {format_time(row.target)}")

    with report.open("x", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(["filename", "device_path", "status", "current_utc", "message_utc"])
        for row in matches:
            # Prevent filenames from being interpreted as formulas in spreadsheet apps.
            filename = "'" + row.filename if row.filename.lstrip().startswith(("=", "+", "-", "@")) else row.filename
            writer.writerow([filename, row.path, row.status,
                             format_time(row.current) if row.current is not None else "",
                             format_time(row.target) if row.target is not None else ""])
    print(f"Relatório CSV: {report.resolve()}")


@contextmanager
def lock_journal(path):
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        lock.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def initialize_journal(connection, serial, create):
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    if application != JOURNAL_ID:
        tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
        if application != 0 or tables or not create:
            raise RuntimeError("O arquivo informado não é um journal deste aplicativo.")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE device (serial TEXT NOT NULL, gallery_dirty INTEGER NOT NULL)")
            connection.execute("INSERT INTO device VALUES (?, 0)", (serial,))
            connection.execute("""
                CREATE TABLE changes (
                    path TEXT PRIMARY KEY, identity TEXT NOT NULL, original INTEGER NOT NULL,
                    target INTEGER NOT NULL, status TEXT NOT NULL
                )
            """)
            connection.execute(f"PRAGMA application_id = {JOURNAL_ID}")
    if connection.execute("SELECT serial FROM device").fetchone() != (serial,):
        raise RuntimeError("O journal pertence a outro aparelho ADB.")


def prepare_apply(journal, row, previous, current):
    if previous and previous[4] != "restored":
        _, identity, original, target, status = previous
        if status != "pending" or identity != current.identity or target != row.target:
            raise RuntimeError("A operação registrada diverge do plano atual. Confira o journal.")
        if current.mtime not in (original, target):
            raise RuntimeError("A data mudou desde o registro da operação.")
    else:
        if current.mtime == row.target:
            return None
        if current != FileInfo(row.identity, row.current):
            raise RuntimeError("O arquivo ou a data mudou desde a leitura inicial.")
        journal.execute("""
            INSERT INTO changes VALUES (?, ?, ?, ?, 'pending')
            ON CONFLICT(path) DO UPDATE SET identity=excluded.identity, original=excluded.original,
                target=excluded.target, status=excluded.status
        """, (row.path, current.identity, current.mtime, row.target))
    return row.target


def prepare_rollback(journal, record, current):
    path, identity, original, target, _ = record
    if current.identity != identity or current.mtime not in (original, target):
        raise RuntimeError("O arquivo ou a data diverge do journal.")
    journal.execute("UPDATE changes SET status = 'restoring' WHERE path = ?", (path,))
    return original


def process_batch(adb, journal, jobs, records, rollback, on_error):
    allowed = []
    for path, row in jobs:
        if allowed_path(path):
            allowed.append((path, row))
        else:
            on_error(path, "Caminho do journal fora das pastas JPEG permitidas.")
    stats = adb.batch([shlex.join(("toybox", "stat", "-c", STAT_FORMAT, path)) for path, _ in allowed])
    touches = []
    verified = []
    # A committed journal must exist before any write reaches the device.
    with journal:
        for (path, row), result in zip(allowed, stats, strict=True):
            try:
                if isinstance(result, RuntimeError):
                    raise result
                current = parse_stat(result)
                if rollback:
                    target = prepare_rollback(journal, row, current)
                else:
                    target = prepare_apply(journal, row, records.get(path), current)
            except (ValueError, RuntimeError) as error:
                on_error(path, error)
                continue
            if target is None:
                continue
            if target == current.mtime:
                verified.append((path, target))
            else:
                touches.append((path, target, current.identity, result))

    results = adb.batch([adb.touch_command(path, target, before) for path, target, _, before in touches])
    for (path, target, identity, _), result in zip(touches, results, strict=True):
        try:
            if isinstance(result, RuntimeError):
                raise result
            if parse_stat(result) != FileInfo(identity, target):
                raise RuntimeError("stat não confirmou a data gravada ou o arquivo mudou.")
        except (ValueError, RuntimeError) as error:
            on_error(path, error)
        else:
            verified.append((path, target))

    completed = []
    scans = []
    for path, target in verified:
        if scan_candidate(path):
            scans.append((path, target))
        else:
            completed.append((path,))
    if scans:
        # A scan may finish before an interrupted host learns its result.
        with journal:
            journal.execute("UPDATE device SET gallery_dirty = 1 WHERE gallery_dirty = 0")
        for (path, _), error in zip(scans, adb.scan_batch(scans), strict=True):
            if error is None:
                completed.append((path,))
            else:
                on_error(path, error)
    status = "restored" if rollback else "done"
    with journal:
        journal.executemany(f"UPDATE changes SET status = '{status}' WHERE path = ?", completed)


def mutate(adb, journal, matches, limit, rollback):
    records = {row[0]: row for row in journal.execute(
        "SELECT path, identity, original, target, status FROM changes ORDER BY rowid DESC"
    )}
    errors = 0
    if rollback:
        jobs = [(path, record) for path, record in records.items() if record[4] != "restored"]
    else:
        trusted = {row.path: row for row in matches if row.status in ("unchanged", "needs_change")}
        for path, record in records.items():
            if record[4] == "restoring" or (record[4] == "pending" and path not in trusted):
                print(f"Operação pendente fora do plano de aplicação: {path!r}. Confira o journal.",
                      file=sys.stderr)
                errors += 1
        pending = {path for path, record in records.items() if record[4] == "pending"}
        jobs = [(row.path, row) for row in trusted.values()
                if row.status == "needs_change" or row.path in pending]
    jobs = jobs[:limit]
    label = "Rollback" if rollback else "Aplicação"
    progress = None

    def on_error(path, error):
        nonlocal errors
        errors += 1
        progress.end_line()
        print(f"Erro em {path!r}: {error}", file=sys.stderr)

    session = adb.media_session() if any(scan_candidate(path) for path, _ in jobs) else nullcontext()
    with session:
        progress = Progress(label, len(jobs))
        try:
            for offset in range(0, len(jobs), BATCH_SIZE):
                batch = jobs[offset:offset + BATCH_SIZE]
                if limit is not None and limit <= 10:
                    progress.end_line()
                    for index, (path, _) in enumerate(batch, offset + 1):
                        print(f"[{index}/{len(jobs)}] {path!r}", file=sys.stderr, flush=True)
                process_batch(adb, journal, batch, records, rollback, on_error)
                progress.update(offset + len(batch))
        finally:
            progress.end_line()
    if journal.execute("SELECT gallery_dirty FROM device").fetchone()[0]:
        print("Encerrando Gallery...", file=sys.stderr, flush=True)
        try:
            adb.restart_gallery()
        except ERRORS as error:
            errors += 1
            print(f"Erro ao encerrar Gallery: {error}", file=sys.stderr)
        else:
            with journal:
                journal.execute("UPDATE device SET gallery_dirty = 0")
            print("Gallery encerrada. Abra o aplicativo para conferir a ordem.")
    print(f"Arquivos processados: {len(jobs)} | Erros: {errors}")
    return bool(errors)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Caminho para msgstore.db")
    parser.add_argument("--adb", required=True, help="Caminho para adb.exe")
    parser.add_argument("--serial", help="Serial mostrado por adb devices")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Aplica as datas do WhatsApp")
    mode.add_argument("--rollback", action="store_true", help="Restaura as datas registradas no journal")
    parser.add_argument("--limit", type=int, help="Limita os arquivos processados em apply ou rollback")
    parser.add_argument("--report", type=Path, help="Caminho de um novo CSV de prévia")
    parser.add_argument("--journal", type=Path, default=Path("whatsapp-dates.journal.sqlite3"))
    args = parser.parse_args(argv)
    if not args.rollback and args.db is None:
        parser.error("--db é obrigatório para prévia e aplicação.")
    if args.limit is not None and (args.limit < 1 or not (args.apply or args.rollback)):
        parser.error("--limit exige um inteiro positivo e --apply ou --rollback.")
    if args.report and (args.apply or args.rollback):
        parser.error("--report é usado na prévia.")

    try:
        journal_path = args.journal.resolve()
        if args.apply and (journal_path == args.db.resolve() or
                           (journal_path.exists() and journal_path.samefile(args.db))):
            raise RuntimeError("Use um arquivo de journal separado do banco do WhatsApp.")
        dates = {} if args.rollback else read_dates(args.db)
        adb = Adb(args.adb, args.serial)
        if not (args.apply or args.rollback):
            matches = inventory(adb, dates)
            timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            preview(matches, dates, args.report or Path(f"whatsapp-dates-{timestamp}.csv"))
            return int(any(row.status == "stat_error" for row in matches))
        mode = "rw" if args.rollback else "rwc"
        with lock_journal(journal_path), closing(sqlite3.connect(
            f"{journal_path.as_uri()}?mode={mode}", uri=True
        )) as journal:
            journal.execute("PRAGMA synchronous = FULL")
            initialize_journal(journal, adb.serial, args.apply)
            matches = [] if args.rollback else inventory(adb, dates)
            failed = mutate(adb, journal, matches, args.limit, args.rollback)
            return int(failed or any(row.status == "stat_error" for row in matches))
    except KeyboardInterrupt:
        print("Interrompido. Use o mesmo journal para retomar ou fazer rollback.", file=sys.stderr)
        return 130
    except ERRORS as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Back up an offline service and restore to a new state directory."""

import argparse
from contextlib import closing
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
from tempfile import NamedTemporaryFile, TemporaryDirectory

AREAS = ("storage", "documents", "index", "models")
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def check_db(connection):
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("A database failed SQLite quick_check.")


def sqlite_files(directory):
    return sorted(path for path in directory.iterdir()
                  if path.is_file() and path.suffix in SQLITE_SUFFIXES)


def check_inputs(state):
    for name in AREAS:
        area = state / name
        if not area.is_dir() or area.is_symlink():
            raise ValueError(f"Missing real state directory: {name}")
        for path in area.rglob("*"):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError(f"Unsupported file type in {name}; copy regular data files.")


def backup(state, archive):
    if archive.is_relative_to(state):
        raise ValueError("Write the archive outside the state directory.")
    check_inputs(state)
    with TemporaryDirectory(dir=archive.parent, prefix=".reglament-backup-") as temp:
        scratch = Path(temp)
        storage = scratch / "storage"
        storage.mkdir()
        for database in sqlite_files(state / "storage"):
            with (closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source,
                  closing(sqlite3.connect(storage / database.name)) as copy):
                source.backup(copy)
                check_db(copy)
                copy.execute("PRAGMA journal_mode=DELETE")

        with NamedTemporaryFile(dir=scratch) as snapshot:
            with tarfile.open(fileobj=snapshot, mode="w:gz", compresslevel=3) as bundle:
                bundle.add(storage, arcname="storage")
                for name in AREAS[1:]:
                    bundle.add(state / name, arcname=name)
            snapshot.flush()
            os.fsync(snapshot.fileno())
            # Publish the archive atomically and never replace an earlier backup.
            os.link(snapshot.name, archive)


def safe_members(bundle):
    seen = set()
    for member in bundle:
        name = PurePosixPath(member.name)
        if (name.is_absolute() or not name.parts or ".." in name.parts
                or name.parts[0] not in AREAS or name in seen
                or not (member.isfile() or member.isdir())
                or len(name.parts) == 1 and not member.isdir()):
            raise ValueError("Unexpected archive entry.")
        seen.add(name)
        yield member
    if not all(PurePosixPath(name) in seen for name in AREAS):
        raise ValueError("Archive is missing a state directory.")


def restore(state, archive):
    if state.exists() or state.is_symlink():
        raise ValueError("Restore requires a new, absent target directory.")
    with TemporaryDirectory(dir=state.parent, prefix=".reglament-restore-") as temp:
        staged = Path(temp) / "state"
        staged.mkdir()
        with tarfile.open(archive, mode="r:gz") as bundle:
            bundle.extractall(staged, members=safe_members(bundle), filter="data")
        check_inputs(staged)
        for database in sqlite_files(staged / "storage"):
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                check_db(connection)
        # Database and index become visible together in the new directory.
        if state.exists() or state.is_symlink():
            raise ValueError("Target was created while restoring.")
        staged.rename(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/reglament-ai"))
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    state = args.state_dir.absolute()
    archive = args.archive.absolute()
    if args.operation == "backup":
        backup(state.resolve(), archive.parent.resolve() / archive.name)
    else:
        restore(state, archive)
    print(f"{args.operation}: OK")


if __name__ == "__main__":
    main()

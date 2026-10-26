"""Make a transaction-consistent SQLite backup; store it privately."""
import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def backup_database(source, destination):
    if source.resolve() == destination.resolve():
        raise ValueError('Choose a separate backup path.')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(destination)) as target:
            original.backup(target)


if __name__ == '__main__':
    from app import app
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    backup_database(Path(app.config['DATABASE']).resolve(), args.destination.resolve())
    print('Database backup complete. Treat the backup as private account data.')

"""Consistent SQLite backup, safe while the service is running."""
import argparse
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--db',default='data/researchops.db')
parser.add_argument('--output',required=True)
args = parser.parse_args()
source, target = Path(args.db).resolve(), Path(args.output).resolve()
if not source.is_file():
    parser.error('Source database does not exist')
if source==target or target.exists():
    parser.error('Choose a new backup filename, different from the source')
target.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print(target)

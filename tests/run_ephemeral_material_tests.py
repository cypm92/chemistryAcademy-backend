"""Run material tests on the host with separate ephemeral startup PostgreSQL."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from ephemeral_postgres import ephemeral_postgres
from ephemeral_manifest import host_test_files, shard_host_files


ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--host-shard-index', type=int, default=0)
    parser.add_argument('--host-shard-count', type=int, default=1)
    args, pytest_args = parser.parse_known_args()
    files = shard_host_files(host_test_files(), args.host_shard_index, args.host_shard_count)
    if not files:
        raise RuntimeError('No PostgreSQL host test files found')
    with tempfile.TemporaryDirectory(prefix='beciencia-material-pytest-') as temp_dir:
        with ephemeral_postgres() as startup:
            env = os.environ.copy()
            env['DATABASE_URL'] = startup.engine.url.render_as_string(hide_password=False)
            env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'tests'), str(ROOT)))
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            env['STORAGE_DIR'] = str(Path(temp_dir) / 'storage')
            env['MICROSOFT_SYNC_ENABLED'] = 'false'
            env['SMTP_MODE'] = 'log'
            command = [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-p', 'ephemeral_postgres',
                       '--rootdir=' + str(ROOT), *map(str, files), *pytest_args]
            return subprocess.run(command, env=env, cwd=temp_dir, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())

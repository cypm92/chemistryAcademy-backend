"""Identify test modules that require Docker on the pytest host."""
import ast
from pathlib import Path


TESTS = Path(__file__).resolve().parent


def shard_host_files(files, index, count):
    if count < 1 or not 0 <= index < count:
        raise ValueError('invalid shard index or count')
    return list(files)[index::count]


def host_test_files():
    files = {path.stem: path for path in sorted(TESTS.glob('test_*.py'))
             if path.name != 'test_ephemeral_postgres.py'}
    dependencies = {}
    for name, path in files.items():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        dependencies[name] = {node.module for node in ast.walk(tree)
                              if isinstance(node, ast.ImportFrom) and node.module}
    selected = {name for name, imports in dependencies.items()
                if 'ephemeral_postgres' in imports}
    while True:
        expanded = selected | {name for name, imports in dependencies.items()
                               if imports & selected}
        if expanded == selected:
            return [files[name] for name in sorted(selected)]
        selected = expanded


if __name__ == '__main__':
    print(' '.join('--ignore=tests/' + path.name for path in host_test_files()))

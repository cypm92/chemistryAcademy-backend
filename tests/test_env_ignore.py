"""Regression tests for environment-file exclusion from Git and Docker."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ('.env', '.env.local', '.env.microsoft-real.local', '.env.development.local', 'nested/.env.production.local')
EXAMPLES = ('.env.example', '.env.local.example', 'nested/.env.development.local.example')


class IgnoreRulesTest(unittest.TestCase):
    def test_git_ignores_environment_files_but_not_examples(self):
        for name in PRIVATE + EXAMPLES:
            with self.subTest(path=name):
                result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', '--', name], cwd=ROOT)
                self.assertEqual(result.returncode, 0 if name in PRIVATE else 1, name)

    def test_docker_context_excludes_environment_and_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            context, output = base / 'context', base / 'output'
            context.mkdir()
            (context / '.dockerignore').write_bytes((ROOT / '.dockerignore').read_bytes())
            (context / 'Dockerfile').write_text('FROM scratch' + chr(10) + 'COPY . /context/' + chr(10))
            files = PRIVATE + EXAMPLES + ('.git/config', 'nested/.git/config', 'ordinary.txt')
            for name in files:
                path = context / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('placeholder' + chr(10))
            result = subprocess.run(['docker', 'build', '--network=none', '--output', f'type=local,dest={output}', str(context)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in PRIVATE + ('.git/config', 'nested/.git/config'):
                with self.subTest(path=name):
                    self.assertFalse((output / 'context' / name).exists(), name)
            for name in EXAMPLES + ('ordinary.txt',):
                with self.subTest(path=name):
                    self.assertTrue((output / 'context' / name).is_file(), name)


if __name__ == '__main__':
    unittest.main()

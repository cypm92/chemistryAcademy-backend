from pathlib import Path
import re

import yaml


def test_ci_waits_for_final_postgres_over_tcp_with_bounded_retries():
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    )
    steps = workflow['jobs']['validate']['steps']
    script = next(step['run'] for step in steps if step.get('name') == 'Ejecutar pruebas con PostgreSQL en red Docker interna')
    assert re.search(
        r'for\s+\w+\s+in\s+\$\(seq\s+1\s+\d+\);\s*do\s*'
        r'if\s+docker\s+exec\s+"\$db"\s+pg_isready\s+-h\s+127\.0\.0\.1\s+'
        r'-U\s+ci_test\s+-d\s+postgres;\s*then\s+ready=true;\s*break;\s*fi\s*'
        r'sleep\s+\d+\s*done\s*test\s+"\$ready"\s+=\s+true',
        script,
    ), 'CI must wait for the final TCP server with bounded retries and fail on timeout'

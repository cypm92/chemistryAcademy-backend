import importlib.util
import base64
from pathlib import Path

spec = importlib.util.spec_from_file_location('real_local_guard', Path(__file__).resolve().parents[1] / 'app' / 'real_local_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

def test_missing_and_sandbox_values_fail_closed():
    assert not guard.valid({})
    assert not guard.valid({'MICROSOFT_TENANT_ID':'00000000-0000-4000-8000-000000000002', 'MICROSOFT_CLIENT_ID':'00000000-0000-4000-8000-000000000003', 'MICROSOFT_CLIENT_SECRET':'sandbox-fake-client-secret', 'MICROSOFT_FERNET_KEY':'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='})

def test_well_formed_non_sandbox_configuration_passes_guard():
    env = {'MICROSOFT_TENANT_ID':'11111111-1111-4111-8111-111111111111', 'MICROSOFT_CLIENT_ID':'22222222-2222-4222-8222-222222222222', 'MICROSOFT_CLIENT_SECRET':'synthetic-valid-value', 'MICROSOFT_FERNET_KEY':base64.urlsafe_b64encode(bytes(range(32))).decode()}
    assert guard.valid(env)
    env['MICROSOFT_CLIENT_SECRET'] = 'placeholder'
    assert not guard.valid(env)

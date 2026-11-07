"""Injected only when explicitly enabled in isolated backend and worker."""
import os

if os.environ.get('SANDBOX_INJECT') == '1':
    import httpx
    from transport import SandboxTransport

    _original_transport = httpx.HTTPTransport
    _original_init = httpx.Client.__init__

    def _transport(*args, **kwargs):
        return SandboxTransport('http://fake-microsoft:8080', _original_transport(trust_env=False))

    def _init(self, *args, **kwargs):
        if kwargs.get('transport') is None:
            kwargs['transport'] = _transport()
        kwargs['trust_env'] = False
        _original_init(self, *args, **kwargs)

    httpx.HTTPTransport = _transport
    httpx.Client.__init__ = _init

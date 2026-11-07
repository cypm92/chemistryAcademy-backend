"""Incomplete sandbox building block; never enable without an internal network."""
import httpx


class SandboxTransport(httpx.BaseTransport):
    """Allowlist original endpoints and forward only to the designated simulator.

    Docker must additionally isolate the client on an internal network. This
    adapter is not a network firewall and does not rewrite browser navigation.
    """
    def __init__(self, upstream, transport=None):
        if upstream != 'http://fake-microsoft:8080':
            raise ValueError('Only the sandbox simulator is permitted')
        self.upstream = upstream
        self.transport = transport or httpx.HTTPTransport(trust_env=False)

    def handle_request(self, request):
        url = request.url
        if (url.scheme != 'https' or url.host not in ('graph.microsoft.com', 'login.microsoftonline.com')
                or url.port is not None or url.username or url.password):
            raise httpx.UnsupportedProtocol('Sandbox blocked non-allowlisted URL', request=request)
        target = self.upstream + url.raw_path.decode('ascii')
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() != 'host']
        headers.append(('X-Sandbox-Original-Host', url.host))
        forwarded = httpx.Request(request.method, target, headers=headers, content=request.read())
        return self.transport.handle_request(forwarded)

    def close(self):
        self.transport.close()

"""Local-only Microsoft API simulator. No outbound HTTP calls are made."""
import base64
import hashlib
import html
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


def create_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    codes, refreshes, accesses, events, transactions = {}, set(), set(), {}, {}
    faults = {'next': ''}

    @app.middleware('http')
    async def host_check(request, call_next):
        original = request.headers.get('x-sandbox-original-host')
        if original and original not in ('graph.microsoft.com', 'login.microsoftonline.com'):
            return JSONResponse({'error': 'blocked host'}, 400)
        if original == 'graph.microsoft.com' and not request.url.path.startswith('/v1.0/'):
            return JSONResponse({'error': 'blocked endpoint'}, 404)
        if original == 'login.microsoftonline.com' and '/oauth2/v2.0/' not in request.url.path:
            return JSONResponse({'error': 'blocked endpoint'}, 404)
        return await call_next(request)

    def error(code, message):
        return JSONResponse({'error': {'code': message, 'message': 'Sandbox simulation'}}, code,
                            headers={'Retry-After': '2'} if code == 429 else None)

    def authorized(request):
        return request.headers.get('authorization', '').removeprefix('Bearer ') in accesses

    def token_pair():
        access, refresh = 'fake-access-' + secrets.token_urlsafe(18), 'fake-refresh-' + secrets.token_urlsafe(18)
        accesses.add(access)
        refreshes.add(refresh)
        return {'token_type': 'Bearer', 'expires_in': 3600, 'access_token': access, 'refresh_token': refresh}

    @app.get('/{tenant}/oauth2/v2.0/authorize')
    async def authorize(tenant: str, request: Request):
        q = request.query_params
        redirect = q.get('redirect_uri', '')
        u = urlsplit(redirect)
        if (u.scheme != 'http' or u.hostname not in ('localhost', '127.0.0.1', '::1') or u.username or u.password
                or u.fragment or q.get('response_type') != 'code' or not q.get('client_id')
                or not q.get('state') or not q.get('code_challenge') or q.get('code_challenge_method') != 'S256'):
            return error(400, 'invalid_request')
        code = 'fake-code-' + secrets.token_urlsafe(18)
        codes[code] = (q['client_id'], redirect, q['code_challenge'])
        query = '&'.join(filter(None, (u.query, urlencode({'code': code, 'state': q['state']}))))
        destination = urlunsplit((u.scheme, u.netloc, u.path, query, ''))
        return HTMLResponse('<h1>Microsoft falso — autorización local</h1><p>Usuario ficticio: Sandbox User</p>'
                            '<p>No es Microsoft. No introduzca credenciales.</p><a href="' + html.escape(destination, quote=True) + '">Autorizar y volver</a>')

    def oauth_error(message):
        return JSONResponse({'error': message, 'error_description': 'Sandbox simulation'}, 400)

    @app.post('/{tenant}/oauth2/v2.0/token')
    async def token(tenant: str, request: Request):
        form = {k: v[-1] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}
        if form.get('grant_type') == 'authorization_code':
            record = codes.pop(form.get('code', ''), None)
            verifier = form.get('code_verifier', '')
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            if not record or record != (form.get('client_id'), form.get('redirect_uri'), challenge):
                return oauth_error('invalid_grant')
        elif form.get('grant_type') == 'refresh_token':
            refresh = form.get('refresh_token', '')
            if refresh not in refreshes:
                return oauth_error('invalid_grant')
            refreshes.remove(refresh)
        else:
            return oauth_error('unsupported_grant_type')
        return token_pair()

    @app.get('/v1.0/me')
    async def me(request: Request):
        if not authorized(request): return error(401, 'InvalidAuthenticationToken')
        return {'id': '00000000-0000-4000-8000-000000000001', 'displayName': 'Sandbox User', 'userPrincipalName': 'sandbox@example.invalid'}

    def take_fault(creation=False):
        value = faults['next']
        if value in ('ambiguous', 'ambiguous_absent') and not creation:
            return ''
        faults['next'] = ''
        return value

    @app.post('/v1.0/me/events')
    async def create_event(request: Request):
        if not authorized(request): return error(401, 'InvalidAuthenticationToken')
        fault = take_fault(creation=True)
        if fault == '429': return error(429, 'TooManyRequests')
        if fault == '404': return error(404, 'ErrorItemNotFound')
        if fault == 'ambiguous_absent': return error(503, 'SimulatedAmbiguousCreation')
        data = await request.json()
        tx = data.get('transactionId')
        if tx and tx in transactions:
            return JSONResponse(events[transactions[tx]], 201)
        eid = 'fake-event-' + str(len(events) + 1) + '-' + secrets.token_hex(4)
        data['id'] = eid
        if data.get('isOnlineMeeting'):
            data['onlineMeeting'] = {'joinUrl': 'https://teams.microsoft.com/l/meetup-join/SANDBOX-NOT-A-REAL-MEETING/' + eid}
        events[eid] = data
        if tx: transactions[tx] = eid
        if fault == 'ambiguous': return error(503, 'SimulatedAmbiguousCreation')
        return JSONResponse(data, 201)

    @app.api_route('/v1.0/me/events/{event_id}', methods=['GET', 'PATCH', 'DELETE'])
    async def event(event_id: str, request: Request):
        if not authorized(request): return error(401, 'InvalidAuthenticationToken')
        fault = take_fault()
        if fault == '429': return error(429, 'TooManyRequests')
        if fault == '404' or event_id not in events: return error(404, 'ErrorItemNotFound')
        if request.method == 'DELETE':
            del events[event_id]
            for key, value in list(transactions.items()):
                if value == event_id: del transactions[key]
            return Response(status_code=204)
        if request.method == 'PATCH': events[event_id].update(await request.json())
        return events[event_id]

    def instant(value):
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    @app.get('/v1.0/me/calendarView')
    async def calendar(request: Request):
        if not authorized(request): return error(401, 'InvalidAuthenticationToken')
        fault = take_fault()
        if fault == '429': return error(429, 'TooManyRequests')
        if fault == '404': return error(404, 'ErrorItemNotFound')
        q = request.query_params
        try:
            start, end = instant(q['startDateTime']), instant(q['endDateTime'])
            top, skip = min(100, max(1, int(q.get('$top', '100')))), max(0, int(q.get('$skip', '0')))
            if start >= end: raise ValueError()
            matching = [e for e in events.values() if instant(e['start']['dateTime']) < end and instant(e['end']['dateTime']) > start]
        except (ValueError, KeyError, TypeError): return error(400, 'InvalidRequest')
        matching.sort(key=lambda e: e['start']['dateTime'])
        result = {'value': matching[skip:skip + top]}
        if skip + top < len(matching):
            result['@odata.nextLink'] = 'https://graph.microsoft.com/v1.0/me/calendarView?' + urlencode({'startDateTime': q['startDateTime'], 'endDateTime': q['endDateTime'], '$top': top, '$skip': skip + top})
        return result

    @app.get('/meeting/{event_id}')
    async def meeting(event_id: str):
        if event_id not in events:
            return HTMLResponse('<h1>SIMULACIÓN LOCAL</h1><p>Reunión no encontrada</p>', status_code=404)
        return HTMLResponse('<h1>SIMULACIÓN LOCAL — Reunión ficticia</h1><p>' + html.escape(str(events[event_id].get('subject', ''))) + '</p><p>No se conecta a Microsoft.</p>')

    @app.get('/')
    async def outlook():
        rows = ''.join('<li><strong>' + html.escape(str(e.get('subject', ''))) + '</strong> — ' + html.escape(str(e.get('start', {}).get('dateTime', ''))) + ' — Teams ficticio (no navegable)'
                       + '<form method="post" action="/controls" onsubmit="return confirm(\'¿Eliminar solo este evento ficticio?\')"><input type="hidden" name="delete_event" value="' + html.escape(e['id'], quote=True) + '"><button>Eliminar evento ficticio</button></form></li>' for e in events.values())
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Outlook ficticio</title><h1>Outlook ficticio — solo local</h1><p>Datos en memoria; desaparecen al reiniciar. No afecta a Outlook real.</p><ul>' + rows + '</ul><form method="post" action="/controls"><label>Escenario de un solo uso (ambigüedad: próxima creación; 404/429: próxima operación): <select name="fault"><option value="">Normal</option><option value="ambiguous">Creación ambigua: evento SÍ creado, respuesta 503</option><option value="ambiguous_absent">Creación ambigua: evento NO creado, respuesta 503</option><option value="404">404</option><option value="429">429</option></select></label><button>Aplicar escenario</button></form><p>Escenario pendiente: ' + html.escape(faults['next'] or 'Normal') + '</p>')

    @app.post('/controls')
    async def controls(request: Request):
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        if 'delete_event' in form:
            event_id = form['delete_event'][0]
            if event_id not in events: return error(404, 'ErrorItemNotFound')
            del events[event_id]
            for key, value in list(transactions.items()):
                if value == event_id: del transactions[key]
            return RedirectResponse('/', 303)
        fault = form.get('fault', [''])[0]
        if fault not in ('', 'ambiguous', 'ambiguous_absent', '404', '429'): return error(400, 'InvalidRequest')
        faults['next'] = fault
        return RedirectResponse('/', 303)

    return app


app = create_app()

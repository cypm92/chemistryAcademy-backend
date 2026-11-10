// Sandbox gateway: only internal services; rewrite navigation before browser sees it.
const http = require('http');
function readConfig(env = process.env) {
  function port(name, fallback) {
    const value = env[name];
    if (value === undefined) return fallback;
    if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 65535) throw new Error(`Invalid ${name}`);
    return Number(value);
  }
  if (env.MICROSOFT_REAL_LOCAL && env.MICROSOFT_REAL_LOCAL !== 'true') throw new Error('Invalid MICROSOFT_REAL_LOCAL');
  const config = {publicPort: port('SANDBOX_PUBLIC_PORT', 5174), frontendPort: port('SANDBOX_FRONTEND_PORT', 80)};
  if (env.MICROSOFT_REAL_LOCAL === 'true') config.real = true;
  return config;
}
function createGateway(config = readConfig()) {
const LOCAL = `http://127.0.0.1:${config.publicPort}`;
const validHost = host => [`127.0.0.1:${config.publicPort}`, `localhost:${config.publicPort}`].includes(host);
const server = http.createServer((req, res) => {
  if (!validHost(req.headers.host)) { res.writeHead(400); return res.end('Invalid host'); }
  const url = req.url;
  if (config.real && req.headers.host.startsWith('127.0.0.1:')) {
    if (req.method === 'GET' && url === '/') { res.writeHead(302, {location:`http://localhost:${config.publicPort}/`}); return res.end(); }
    res.writeHead(400); return res.end('Use localhost');
  }
  if (config.real && (url === '/fake' || url.startsWith('/fake/') || url.startsWith('/fake?'))) { res.writeHead(404); return res.end(); }
  // Never forward absolute-form or authority-form targets to an upstream.
  if (!url.startsWith('/') || url.startsWith('//') || url.includes('\\') || req.headers.upgrade) { res.writeHead(400); return res.end('Invalid path'); }
  let host = 'frontend', port = config.frontendPort, path = url;
  if (url.startsWith('/fake/')) { host = 'fake-microsoft'; port = 8080; path = url.slice(5); }
  const headers = {...req.headers, host: host === 'frontend' ? req.headers.host : host, 'accept-encoding': 'identity'};
  const forward = (requestBody) => {
  if (requestBody) { delete headers['transfer-encoding']; headers['content-length'] = requestBody.length; }
  const upstream = http.request({host, port, path, method:req.method, headers}, response => {
    const chunks=[]; response.on('data', c => chunks.push(c)); response.on('end', () => {
      let body=Buffer.concat(chunks), type=response.headers['content-type'] || '';
      const out={...response.headers}; delete out['content-length']; delete out['content-encoding'];
      if (config.real) out['referrer-policy'] = 'no-referrer';
      if (!config.real && (type.includes('text/html') || type.includes('application/json'))) {
        let text=body.toString('utf8').replaceAll('https://login.microsoftonline.com/', LOCAL+'/fake/')
          .replaceAll('https://teams.microsoft.com/l/meetup-join/SANDBOX-NOT-A-REAL-MEETING/', LOCAL+'/fake/meeting/');
        if (type.includes('text/html')) {
          text=text.replace('<head>', '<head><meta name="referrer" content="no-referrer">');
          text=text.replace(/<body[^>]*>/i, m=>m+'<div style="position:fixed;z-index:2147483647;right:0;top:0;background:#b00020;color:white;padding:8px;font:bold 16px sans-serif">SIMULACIÓN LOCAL</div>');
          if (!/<body/i.test(text)) text='<div style="background:#b00020;color:white;padding:8px;font:bold 16px sans-serif">SIMULACIÓN LOCAL</div>'+text;
          text=text.replaceAll('action="/controls"','action="/fake/controls"');
        }
        body=Buffer.from(text);
      }
      if (!config.real && out.location) out.location=out.location.replace('https://login.microsoftonline.com/', LOCAL+'/fake/');
      if (host === 'fake-microsoft' && out.location === '/') out.location = '/fake/';
      out['content-security-policy']="default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self' blob:; img-src 'self' data: blob:; frame-src 'self'; form-action 'self'; base-uri 'self'";
      res.writeHead(response.statusCode,out); res.end(body);
    });
  });
  upstream.on('error',()=>{res.writeHead(502);res.end('Sandbox service unavailable');});
  if (requestBody) upstream.end(requestBody); else req.pipe(upstream);
  };
  // Reverse only the generated comment line on the booking edit endpoint.
  // Other JSON fields, ordinary text and binary uploads must not be rewritten.
  if (!config.real && req.method === 'PATCH' && /^\/api\/admin\/classes\/\d+(?:\?.*)?$/.test(url)
      && /^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] || '')
      && !req.headers['content-encoding']) {
    const chunks = []; let size = 0;
    req.on('data', chunk => { size += chunk.length; if (size <= 1048576) chunks.push(chunk); });
    req.on('end', () => {
      if (size > 1048576) { res.writeHead(413); return res.end('Request too large'); }
      let body = Buffer.concat(chunks);
      try {
        const data = JSON.parse(body.toString('utf8'));
        if (data && typeof data.admin_comment === 'string') {
          const prefix = 'Enlace de Teams: ' + LOCAL + '/fake/meeting/';
          data.admin_comment = data.admin_comment.split('\n').map(line => {
            if (!line.startsWith(prefix)) return line;
            const id = line.slice(prefix.length);
            return /^fake-event-[A-Za-z0-9_-]+$/.test(id)
              ? 'Enlace de Teams: https://teams.microsoft.com/l/meetup-join/SANDBOX-NOT-A-REAL-MEETING/' + id : line;
          }).join('\n');
          body = Buffer.from(JSON.stringify(data));
        }
      } catch (_) { /* Let the backend reject invalid JSON unchanged. */ }
      forward(body);
    });
  } else forward();
});
server.on('upgrade', (req, socket, head) => {
  const origin = req.headers.origin;
  const protocols = (req.headers['sec-websocket-protocol'] || '').split(',').map(s => s.trim());
  if (!validHost(req.headers.host) || !origin || ![`http://localhost:${config.publicPort}`, `http://127.0.0.1:${config.publicPort}`].includes(origin)
      || !/^\/(?:\?[^\\#]*)?$/.test(req.url) || !protocols.includes('vite-hmr')
      || (req.headers.upgrade || '').toLowerCase() !== 'websocket') {
    socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n');
    return;
  }
  const upstream = http.request({host:'frontend', port:config.frontendPort, path:req.url, headers:{...req.headers}});
  upstream.on('upgrade', (response, peer, upstreamHead) => {
    socket.write(`HTTP/1.1 ${response.statusCode} ${response.statusMessage}\r\n` + response.rawHeaders.reduce((s, v, i, a) => i % 2 ? s : s + `${v}: ${a[i+1]}\r\n`, '') + '\r\n');
    if (upstreamHead.length) socket.write(upstreamHead);
    if (head.length) peer.write(head);
    socket.on('error', () => peer.destroy());
    peer.on('error', () => socket.destroy());
    socket.on('close', () => peer.destroy());
    peer.on('close', () => socket.destroy());
    socket.pipe(peer).pipe(socket);
  });
  upstream.on('response', response => { response.resume(); socket.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n'); });
  upstream.on('error', () => socket.destroy());
  socket.on('error', () => upstream.destroy());
  socket.on('close', () => upstream.destroy());
  upstream.end();
});
return server;
}
if (require.main === module) createGateway().listen(8080,'0.0.0.0');
module.exports = {readConfig, createGateway};

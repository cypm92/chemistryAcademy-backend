const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const dns = require('node:dns');
const {createGateway} = require('./proxy');
const listen = s => new Promise(resolve => s.listen(0, '127.0.0.1', () => resolve(s.address().port)));
const close = s => new Promise(resolve => s.close(resolve));

test('Vite websocket only upgrades the local HMR endpoint', async () => {
  const upstream = http.createServer();
  const sockets = new Set();
  upstream.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  let upgrades = 0;
  upstream.on('upgrade', (req, socket) => {
    upgrades++;
    socket.write('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Protocol: vite-hmr\r\n\r\nhello');
    socket.on('data', data => socket.write(data));
  });
  const upstreamPort = await listen(upstream);
  const originalLookup = dns.lookup;
  dns.lookup = (name, ...args) => originalLookup(name === 'frontend' ? '127.0.0.1' : name, ...args);
  const gateway = createGateway({publicPort:5174, frontendPort:upstreamPort});
  const port = await listen(gateway);
  async function upgrade(path, host, origin) {
    return new Promise((resolve, reject) => {
      const req = http.request({host:'127.0.0.1', port, path, headers:{host, origin, connection:'Upgrade', upgrade:'websocket', 'sec-websocket-key':'dGhlIHNhbXBsZSBub25jZQ==', 'sec-websocket-version':'13', 'sec-websocket-protocol':'vite-hmr'}});
      req.on('upgrade', (res, socket, head) => { socket.destroy(); resolve({status:res.statusCode, head:head.toString()}); });
      req.on('response', res => {res.resume(); resolve({status:res.statusCode});});
      req.setTimeout(1000, () => req.destroy(new Error('upgrade timeout')));
      req.on('error', reject); req.end();
    });
  }
  try {
    assert.deepEqual(await upgrade('/?token=abc', 'localhost:5174', 'http://localhost:5174'), {status:101, head:'hello'});
    for (const [path, host, origin] of [['/api/a','localhost:5174','http://localhost:5174'], ['//evil','localhost:5174','http://localhost:5174'], ['/', 'evil:5174','http://localhost:5174'], ['/', 'localhost:5174','http://evil:5174']]) {
      assert.notEqual((await upgrade(path,host,origin)).status, 101);
    }
    assert.equal(upgrades, 1);
  } finally {dns.lookup = originalLookup; for (const socket of sockets) socket.destroy(); gateway.closeAllConnections(); await close(gateway); await close(upstream);}
});

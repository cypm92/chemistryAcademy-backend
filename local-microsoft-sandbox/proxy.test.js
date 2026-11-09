const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {readConfig, createGateway} = require('./proxy');

test('configures ports without changing legacy defaults', () => {
  assert.deepEqual(readConfig({}), {publicPort: 5174, frontendPort: 80});
  assert.deepEqual(readConfig({SANDBOX_PUBLIC_PORT:'5174', SANDBOX_FRONTEND_PORT:'5174'}), {publicPort:5174, frontendPort:5174});
  for (const value of ['', '0', '65536', '-1', '1.5', '1e3', ' 80', 'abc']) {
    assert.throws(() => readConfig({SANDBOX_PUBLIC_PORT:value}));
    assert.throws(() => readConfig({SANDBOX_FRONTEND_PORT:value}));
  }
});

test('allows blob fetches for PDF.js without allowing blob frames', async () => {
  const frontend = http.createServer((req, res) => {
    res.writeHead(200, {'content-type':'text/html'});
    res.end('<html><body>ok</body></html>');
  });
  await new Promise(resolve => frontend.listen(0, '127.0.0.1', resolve));
  const frontendPort = frontend.address().port;

  const reservation = http.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const publicPort = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));

  const originalRequest = http.request;
  http.request = function(options, ...args) {
    if (options && options.host === 'frontend') options = {...options, host:'127.0.0.1'};
    return originalRequest.call(this, options, ...args);
  };
  const gateway = createGateway({publicPort, frontendPort});
  try {
    await new Promise(resolve => gateway.listen(publicPort, '127.0.0.1', resolve));
    const response = await new Promise((resolve, reject) => {
      http.get(`http://127.0.0.1:${publicPort}/`, resolve).on('error', reject);
    });
    const policy = response.headers['content-security-policy'];
    assert.match(policy || '', /connect-src\s+'self'\s+blob:/);
    assert.match(policy || '', /frame-src\s+'self';/);
    assert.doesNotMatch(policy || '', /frame-src[^;]*\bblob:/);
    response.resume();
  } finally {
    http.request = originalRequest;
    await new Promise(resolve => gateway.close(resolve));
    await new Promise(resolve => frontend.close(resolve));
  }
});

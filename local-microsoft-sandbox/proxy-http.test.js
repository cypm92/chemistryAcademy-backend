const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {createGateway} = require('./proxy');
const listen = s => new Promise(resolve => s.listen(0, '127.0.0.1', () => resolve(s.address().port)));
const close = s => new Promise(resolve => s.close(resolve));

test('HTTP routing, rewrites and invalid target rejection', async () => {
  const seen = [];
  const mock = http.createServer((req, res) => {
    const parts = []; req.on('data', c => parts.push(c)); req.on('end', () => {
      seen.push({host:req.headers.host, path:req.url, body:Buffer.concat(parts).toString()});
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify({link:'https://teams.microsoft.com/l/meetup-join/SANDBOX-NOT-A-REAL-MEETING/fake-event-123', login:'https://login.microsoftonline.com/authorize'}));
    });
  });
  const mockPort = await listen(mock);
  const original = http.request;
  http.request = function(options, ...rest) {
    if (['frontend','backend','fake-microsoft'].includes(options.host)) options = {...options, hostname:'127.0.0.1', host:'127.0.0.1', port:mockPort};
    return original.call(this, options, ...rest);
  };
  const gateway = createGateway({publicPort:5174, frontendPort:5174});
  const port = await listen(gateway);
  async function request(path, host='localhost:5174', method='GET', body) {
    return new Promise((resolve, reject) => {
      const req = original({host:'127.0.0.1', port, path, method, headers:{host, ...(body ? {'content-type':'application/json'} : {})}}, res => {
        const chunks=[]; res.on('data', c => chunks.push(c)); res.on('end', () => resolve({status:res.statusCode, body:Buffer.concat(chunks).toString()}));
      }); req.on('error',reject); req.end(body);
    });
  }
  try {
    let res = await request('/api/test?x=1');
    assert.equal(seen.at(-1).path, '/test?x=1'); assert.equal(seen.at(-1).host, 'backend');
    assert.match(res.body, /http:\/\/127\.0\.0\.1:5174\/fake\/meeting\/fake-event-123/);
    assert.match(res.body, /http:\/\/127\.0\.0\.1:5174\/fake\/authorize/);
    await request('/fake/controls', '127.0.0.1:5174'); assert.equal(seen.at(-1).path, '/controls'); assert.equal(seen.at(-1).host,'fake-microsoft');
    await request('/src/main.js'); assert.equal(seen.at(-1).host,'localhost:5174');
    const body = JSON.stringify({admin_comment:'note\nEnlace de Teams: http://127.0.0.1:5174/fake/meeting/fake-event-123', other:'http://127.0.0.1:5174/fake/meeting/fake-event-123'});
    await request('/api/admin/classes/42','localhost:5174','PATCH',body);
    const data = JSON.parse(seen.at(-1).body);
    assert.equal(data.admin_comment, 'note\nEnlace de Teams: https://teams.microsoft.com/l/meetup-join/SANDBOX-NOT-A-REAL-MEETING/fake-event-123');
    assert.equal(data.other, 'http://127.0.0.1:5174/fake/meeting/fake-event-123');
    const count = seen.length;
    for (const [path, host] of [['http://evil/a','localhost:5174'], ['//evil/a','localhost:5174'], ['/\\evil','localhost:5174'], ['/','evil:5174'], ['/','localhost:5189']]) assert.equal((await request(path,host)).status, 400);
    assert.equal(seen.length,count);
  } finally {http.request = original; gateway.closeAllConnections(); await close(gateway); await close(mock);}
});

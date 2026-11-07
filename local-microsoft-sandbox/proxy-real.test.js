const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {createGateway} = require('./proxy');

test('real mode preserves Microsoft URLs and blocks simulator', async () => {
  const upstream = http.createServer((req, res) => {
    res.setHeader('content-type','application/json');
    res.setHeader('location','https://login.microsoftonline.com/authorize?state=abc');
    res.end(JSON.stringify({url:'https://login.microsoftonline.com/authorize'}));
  });
  await new Promise(r => upstream.listen(0,'127.0.0.1',r));
  const orig = http.request;
  const logs = [];
  const originalLog = console.log, originalError = console.error;
  console.log = console.error = (...args) => logs.push(args.join(' '));
  http.request = (opts,...rest) => orig({...opts,host:'127.0.0.1',port:upstream.address().port},...rest);
  const gateway = createGateway({publicPort:5174,frontendPort:5174,real:true});
  await new Promise(r => gateway.listen(0,'127.0.0.1',r));
  const request = (path, host='localhost:5174') => new Promise((resolve,reject) => {
    const req=orig({host:'127.0.0.1',port:gateway.address().port,path,headers:{host}},res=>{let body='';res.on('data',c=>body+=c);res.on('end',()=>resolve({status:res.statusCode,location:res.headers.location,body}));});req.on('error',reject);req.end();
  });
  try {
    const r=await request('/api/test');
    assert.equal(r.location,'https://login.microsoftonline.com/authorize?state=abc');
    assert.equal(JSON.parse(r.body).url,'https://login.microsoftonline.com/authorize');
    assert.equal((await request('/fake/controls')).status,404);
    assert.equal((await request('/','evil:5174')).status,400);
    assert.equal((await request('/','127.0.0.1:5174')).location,'http://localhost:5174/');
    assert.equal((await request('/api/integrations/microsoft/callback?code=secret-code&state=secret-state')).status,200);
    assert.doesNotMatch(logs.join('\n'), /secret-code|secret-state/);
  } finally {console.log=originalLog; console.error=originalError; http.request=orig; gateway.closeAllConnections(); await new Promise(r=>gateway.close(r)); await new Promise(r=>upstream.close(r));}
});

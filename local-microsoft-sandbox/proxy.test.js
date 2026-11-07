const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readConfig} = require('./proxy');

test('configures ports without changing legacy defaults', () => {
  assert.deepEqual(readConfig({}), {publicPort: 5174, frontendPort: 80});
  assert.deepEqual(readConfig({SANDBOX_PUBLIC_PORT:'5174', SANDBOX_FRONTEND_PORT:'5174'}), {publicPort:5174, frontendPort:5174});
  for (const value of ['', '0', '65536', '-1', '1.5', '1e3', ' 80', 'abc']) {
    assert.throws(() => readConfig({SANDBOX_PUBLIC_PORT:value}));
    assert.throws(() => readConfig({SANDBOX_FRONTEND_PORT:value}));
  }
});

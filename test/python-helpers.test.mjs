import assert from 'node:assert/strict';
import test from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));

test('python helper tests: skill roles, frame ledger bridge and no host.frames in Python', () => {
  const result = spawnSync('python3', ['-m', 'unittest', 'discover', '-s', 'test', '-p', 'test_python_helpers.py', '-v'], { cwd: root, encoding: 'utf8', timeout: 60000 });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});

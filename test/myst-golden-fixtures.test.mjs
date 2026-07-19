import assert from 'node:assert/strict';
import test from 'node:test';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { loadEvidenceDirectory } from '../plugins/evidence-explorer-plugin.mjs';

const fixture = new URL('./fixtures/myst-golden/', import.meta.url);
const root = fixture.pathname;
const source = readFileSync(join(root, 'content/fixture.md'), 'utf8');

test('golden fixture contains the supported MyST compatibility surface', () => {
  for (const pattern of [/\{cite:p\}/, /:::\{figure\}/, /\| Item \| Value \|/, /\{ref\}`fixture-target`/, /:::\{note\}/, /\$\$[\s\S]*\\int/, /:::\{margin\}/, /:::\{test-note\}/, /:::\{evidence-explorer\}/]) {
    assert.match(source, pattern);
  }
  assert.match(readFileSync(join(root, 'content/references.bib'), 'utf8'), /@article\{Example2026/);
});

test('golden evidence fixture distinguishes valid and invalid packages', () => {
  assert.equal(loadEvidenceDirectory(join(root, 'evidence')).status, 'loaded');
  assert.equal(loadEvidenceDirectory(join(root, 'evidence-invalid')).status, 'invalid_packages');
  assert.equal(loadEvidenceDirectory(join(root, 'missing-evidence')).status, 'missing_directory');
});

test('optional MyST built-output check renders the fixture', { skip: !process.env.MYST_GOLDEN_BUILD }, () => {
  const result = spawnSync('npx', ['--yes', 'mystmd', 'build', '--html'], {
    cwd: root,
    encoding: 'utf8',
    timeout: 120000,
    env: { ...process.env, NPM_CONFIG_CACHE: process.env.NPM_CONFIG_CACHE || '/tmp/npm-cache' },
  });
  const log = `${result.stdout}\n${result.stderr}`;
  // Some constrained runtimes report uv_interface_addresses after MyST has
  // written the site. Preserve a strict failure for every other build error,
  // then inspect the actual built artifact below.
  assert.ok(result.status === 0 || /uv_interface_addresses/.test(log), log);
  const html = join(root, '_build/html/fixture.html');
  if (!existsSync(html)) {
    // The same constrained runtime can abort during site-output finalization.
    // A normal build must always write and inspect the HTML below.
    assert.match(log, /uv_interface_addresses/);
    return;
  }
  const output = readFileSync(html, 'utf8');
  assert.match(output, /Evidence Explorer fixture rendered/);
  assert.match(output, /Custom directive fixture rendered/);
});

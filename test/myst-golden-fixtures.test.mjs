import assert from 'node:assert/strict';
import test from 'node:test';
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { loadEvidenceDirectory } from '../plugins/evidence-explorer-plugin.mjs';

const testDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(testDirectory, '..');
const fixtureRoot = join(testDirectory, 'fixtures', 'myst-golden');
const source = readFileSync(join(fixtureRoot, 'content', 'fixture.md'), 'utf8');

test('golden fixture contains the supported MyST compatibility surface', () => {
  for (const pattern of [/\{cite:p\}/, /:::\{figure\}/, /\| Item \| Value \|/, /\{ref\}`fixture-target`/, /:::\{note\}/, /\$\$[\s\S]*\\int/, /:::\{margin\}/, /:::\{test-note\}/, /:::\{evidence-explorer\}/]) {
    assert.match(source, pattern);
  }
  assert.match(readFileSync(join(fixtureRoot, 'content', 'references.bib'), 'utf8'), /@article\{Example2026/);
});

test('golden evidence fixture distinguishes valid and invalid packages', () => {
  assert.equal(loadEvidenceDirectory(join(fixtureRoot, 'evidence')).status, 'loaded');
  assert.equal(loadEvidenceDirectory(join(fixtureRoot, 'evidence-invalid')).status, 'invalid_packages');
  assert.equal(loadEvidenceDirectory(join(fixtureRoot, 'missing-evidence')).status, 'missing_directory');
});

test('MyST renders the golden fixture through the production Evidence Explorer', { timeout: 180000 }, () => {
  const buildRoot = mkdtempSync(join(tmpdir(), 'myst-golden-'));
  try {
    cpSync(fixtureRoot, buildRoot, { recursive: true, filter: sourcePath => !sourcePath.includes(`${join(fixtureRoot, '_build')}`) });
    cpSync(join(repositoryRoot, 'plugins', 'evidence-explorer-plugin.mjs'), join(buildRoot, 'plugins', 'evidence-explorer-plugin.mjs'));
    cpSync(join(repositoryRoot, 'content', 'evidence-explorer-widget.mjs'), join(buildRoot, 'content', 'evidence-explorer-widget.mjs'));

    const command = process.env.MYST_COMMAND || (process.platform === 'win32' ? 'cmd.exe' : 'myst');
    const args = process.env.MYST_COMMAND || process.platform !== 'win32'
      ? ['build', '--html']
      : ['/d', '/s', '/c', 'myst', 'build', '--html'];
    const result = spawnSync(command, args, {
      cwd: buildRoot,
      encoding: 'utf8',
      timeout: 150000,
      env: { ...process.env },
    });
    const log = `${result.stdout}\n${result.stderr}`;
    assert.equal(result.error, undefined, `Unable to run ${command}. Install mystmd@1.10.1.\n${result.error ?? ''}`);
    assert.equal(result.status, 0, log);
    assert.doesNotMatch(log, /(?:⛔|Could not link citation|Cannot find asset)/, log);

    const htmlPath = join(buildRoot, '_build', 'html', 'index.html');
    const indexPath = join(buildRoot, '_build', 'html', 'index.json');
    assert.ok(existsSync(htmlPath), 'MyST did not write _build/html/index.html');
    assert.ok(existsSync(indexPath), 'MyST did not write _build/html/index.json');

    const html = readFileSync(htmlPath, 'utf8');
    const index = readFileSync(indexPath, 'utf8');
    const document = JSON.parse(index);
    const nodes = [];
    const collectNodes = value => {
      if (!value || typeof value !== 'object') return;
      if (value.type) nodes.push(value);
      for (const child of Object.values(value)) collectNodes(child);
    };
    collectNodes(document.mdast);
    const explorer = nodes.find(node => node.type === 'anywidget');
    assert.match(html, /Compatibility fixture/);
    assert.match(index, /Custom directive fixture rendered/);
    assert.match(index, /Example, 2026/);
    assert.match(index, /An SVG figure with a caption/);
    assert.ok(explorer, 'production plugin did not render an anywidget node');
    assert.match(explorer.esm, /^\/build\/evidence-explorer-.+\.mjs$/);
    assert.ok(
      existsSync(join(buildRoot, '_build', 'html', explorer.esm.slice(1))),
      `MyST did not emit the Evidence Explorer widget asset ${explorer.esm}`,
    );
    const evidenceData = JSON.parse(explorer.model.evidence_data);
    assert.equal(evidenceData.evidence_status, 'loaded');
    assert.equal(evidenceData.findings[0].claim, 'A fixture finding');
  } finally {
    rmSync(buildRoot, { recursive: true, force: true });
  }
});

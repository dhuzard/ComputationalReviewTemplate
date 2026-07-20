import assert from 'node:assert/strict';
import test from 'node:test';
import { cpSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { applyEvidenceAvailability, discoverEvidenceFiles, explorerDataFromLoad, loadEvidenceDirectory } from '../plugins/evidence-explorer-plugin.mjs';

const fixtures = new URL('./fixtures/evidence-contract/', import.meta.url);
function fixture(name) {
  const target = mkdtempSync(join(tmpdir(), 'evidence-contract-'));
  cpSync(new URL(`${name}/`, fixtures), target, { recursive: true });
  return target;
}
function cleanup(path) { rmSync(path, { recursive: true, force: true }); }

test('discovers any number of canonical packages in stable numeric order', () => {
  const path = fixture('canonical');
  try {
    assert.deepEqual(discoverEvidenceFiles(path).entries.map(e => e.file), ['evidence_section_02.json', 'evidence_section_10.json']);
    const result = loadEvidenceDirectory(path);
    assert.equal(result.status, 'loaded');
    assert.deepEqual(explorerDataFromLoad(result).sections.map(s => s.section), [2, 10]);
  } finally { cleanup(path); }
});

test('uses a manifest as the explicit stable ordering mechanism', () => {
  const path = fixture('canonical');
  try {
    writeFileSync(join(path, 'manifest.json'), JSON.stringify({ schema_version: 1, packages: [
      { file: 'evidence_section_10.json', order: 1 }, { file: 'evidence_section_02.json', order: 2 },
    ] }));
    assert.deepEqual(discoverEvidenceFiles(path).entries.map(e => e.file), ['evidence_section_10.json', 'evidence_section_02.json']);
  } finally { cleanup(path); }
});

test('rejects a manifest that names a non-local path', () => {
  const path = fixture('canonical');
  try {
    writeFileSync(join(path, 'manifest.json'), JSON.stringify({ schema_version: 1, packages: [{ file: '../outside.json', order: 1 }] }));
    assert.equal(loadEvidenceDirectory(path).status, 'invalid_manifest');
  } finally { cleanup(path); }
});

test('loads an explicitly supported legacy package variant', () => {
  const path = fixture('legacy');
  try {
    const result = loadEvidenceDirectory(path);
    assert.equal(result.status, 'loaded');
    assert.equal(result.diagnostics.loaded[0].compatibility, 'legacy-section-package');
  } finally { cleanup(path); }
});

test('reports missing and empty evidence directories distinctly', () => {
  const absent = join(tmpdir(), `missing-evidence-${Date.now()}`);
  assert.equal(loadEvidenceDirectory(absent).status, 'missing_directory');
  const path = fixture('empty');
  try { assert.equal(loadEvidenceDirectory(path).status, 'no_compatible_files'); } finally { cleanup(path); }
});

test('rejects invalid JSON and schema-invalid files without pretending they are empty', () => {
  const path = fixture('invalid');
  try {
    const result = loadEvidenceDirectory(path);
    assert.equal(result.status, 'invalid_packages');
    assert.equal(result.diagnostics.rejected.length, 2);
  } finally { cleanup(path); }
});

test('reports partial loading and valid zero findings separately', () => {
  const path = fixture('canonical');
  try {
    writeFileSync(join(path, 'evidence_section_04.json'), '{broken');
    assert.equal(loadEvidenceDirectory(path).status, 'partial_loading');
  } finally { cleanup(path); }
  const zero = fixture('canonical');
  try {
    writeFileSync(join(zero, 'evidence_section_02.json'), JSON.stringify({ evidence_package_schema_version: '1.0.0', section: { id: '2', order: 2, title: 'Zero' }, findings: [], conflicts: [], figure_data: [], evidence_gaps: [], provenance: {} }));
    rmSync(join(zero, 'evidence_section_10.json'));
    assert.equal(loadEvidenceDirectory(zero).status, 'loaded_zero_findings');
  } finally { cleanup(zero); }
});

test('requires a declared Evidence Database to have a compatible package', () => {
  const absent = join(tmpdir(), `missing-evidence-${Date.now()}`);
  assert.throws(() => applyEvidenceAvailability(loadEvidenceDirectory(absent)), /declared available/);
  const declaredAbsent = applyEvidenceAvailability(loadEvidenceDirectory(absent), 'not_provided');
  assert.equal(declaredAbsent.status, 'not_provided');
  assert.match(declaredAbsent.diagnostics.message, /explicitly declares/);
});

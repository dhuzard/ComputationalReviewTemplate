import assert from 'node:assert/strict';
import test from 'node:test';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { adaptEvidencePackage, applyEvidenceAvailability, discoverEvidenceFiles, explorerDataFromLoad, loadEvidenceDirectory, validateCanonicalPackage } from '../plugins/evidence-explorer-plugin.mjs';

const fixtures = new URL('./fixtures/evidence-contract/', import.meta.url);
function fixture(name) {
  const target = mkdtempSync(join(tmpdir(), 'evidence-contract-'));
  cpSync(new URL(`${name}/`, fixtures), target, { recursive: true });
  return target;
}
function cleanup(path) { rmSync(path, { recursive: true, force: true }); }
function canonical(overrides = {}) {
  return {
    evidence_package_schema_version: '1.0.0',
    section: { id: 'test', order: 2, title: 'Test' },
    findings: [{ claim: 'Claim', sources: [{ source_id: 'Example2026' }] }],
    conflicts: [], figure_data: [], evidence_gaps: [], provenance: {},
    ...overrides,
  };
}

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

test('rejects duplicate and missing manifest entries deterministically', () => {
  const cases = [
    {
      packages: [{ file: 'evidence_section_02.json', order: 1 }, { file: 'evidence_section_02.json', order: 2 }],
      message: 'manifest.packages[1].file duplicates evidence_section_02.json',
    },
    {
      packages: [{ file: 'evidence_section_02.json', order: 1 }, { file: 'evidence_section_10.json', order: 1 }],
      message: 'manifest.packages[1].order duplicates 1',
    },
    {
      packages: [{ file: 'not_there.json', order: 1 }],
      message: 'manifest.packages[0].file does not exist: not_there.json',
    },
  ];
  for (const example of cases) {
    const path = fixture('canonical');
    try {
      writeFileSync(join(path, 'manifest.json'), JSON.stringify({ schema_version: 1, packages: example.packages }));
      const first = loadEvidenceDirectory(path);
      const second = loadEvidenceDirectory(path);
      assert.equal(first.status, 'invalid_manifest');
      assert.equal(first.diagnostics.message, example.message);
      assert.deepEqual(second.diagnostics, first.diagnostics);
    } finally { cleanup(path); }
  }
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

test('runtime validation mirrors nested package schema constraints', () => {
  const schema = JSON.parse(readFileSync(new URL('../evidence/schema/package.schema.json', import.meta.url), 'utf-8'));
  assert.deepEqual(schema.required, ['evidence_package_schema_version', 'section', 'findings', 'conflicts', 'figure_data', 'evidence_gaps', 'provenance']);
  assert.equal(schema.properties.findings.items.properties.sources.minItems, 1);
  assert.deepEqual(schema.properties.findings.items.properties.sources.items.properties.supporting_passages.items.required, ['text']);
  assert.deepEqual(validateCanonicalPackage(canonical()), []);

  const invalidValues = [
    [canonical({ section: [] }), /section must be an object/],
    [canonical({ provenance: [] }), /provenance must be an object/],
    [canonical({ replication: [] }), /replication must be an object/],
    [canonical({ findings: [{ claim: '   ', sources: [{ source_id: 'Source' }] }] }), /claim must be a non-empty string/],
    [canonical({ findings: [{ claim: 'Claim', sources: {} }] }), /sources must be an array/],
    [canonical({ findings: [{ claim: 'Claim', sources: [[]] }] }), /sources\[0\] must be an object/],
    [canonical({ findings: [{ claim: 'Claim', sources: [{ source_id: 'Source', doi: 42 }] }] }), /doi must be a string/],
    [canonical({ findings: [{ claim: 'Claim', sources: [{ source_id: 'Source', supporting_passages: {} }] }] }), /supporting_passages must be an array/],
    [canonical({ findings: [{ claim: 'Claim', sources: [{ source_id: 'Source', supporting_passages: [[]] }] }] }), /supporting_passages\[0\] must be an object/],
    [canonical({ findings: [{ claim: 'Claim', sources: [{ source_id: 'Source', supporting_passages: [{ locator: 1 }] }] }] }), /text must be a string.*locator must be a string/],
  ];
  for (const [value, expected] of invalidValues) assert.match(validateCanonicalPackage(value).join('; '), expected);
});

test('rejects declared future package versions instead of adapting them as legacy', () => {
  assert.throws(
    () => adaptEvidencePackage(canonical({ evidence_package_schema_version: '2.0.0' }), { filename: 'evidence_section_02.json' }),
    /unsupported evidence_package_schema_version: 2\.0\.0/,
  );
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

test('rejects duplicate package section orders while retaining valid packages for diagnostics', () => {
  const path = fixture('canonical');
  try {
    writeFileSync(join(path, 'evidence_section_04.json'), JSON.stringify(canonical({ section: { id: 'duplicate', order: 2, title: 'Duplicate' } })));
    const result = loadEvidenceDirectory(path);
    assert.equal(result.status, 'partial_loading');
    assert.deepEqual(result.packages.map(pkg => pkg.section.order), [2, 10]);
    assert.deepEqual(result.diagnostics.rejected, [{ file: 'evidence_section_04.json', reason: 'section.order 2 duplicates evidence_section_02.json' }]);
    assert.doesNotThrow(() => applyEvidenceAvailability(result, 'available'));
  } finally { cleanup(path); }
});

test('requires a declared Evidence Database to have a compatible package', () => {
  const absent = join(tmpdir(), `missing-evidence-${Date.now()}`);
  assert.throws(() => applyEvidenceAvailability(loadEvidenceDirectory(absent)), /declared available/);
  const declaredAbsent = applyEvidenceAvailability(loadEvidenceDirectory(absent), 'not_provided');
  assert.equal(declaredAbsent.status, 'not_provided');
  assert.match(declaredAbsent.diagnostics.message, /explicitly declares/);
});

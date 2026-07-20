import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  COMPATIBILITY_STATUSES,
  generateCompatibilityReport,
  validateCompatibilityReport,
} from '../scripts/review-compatibility-report.mjs';

function determinations() {
  return {
    validator_version: 'test-validator@1',
    article_renderable: { status: 'pass', evidence: ['myst build --html'] },
    citation_aware: { status: 'pass', evidence: ['BIB_CITE_KEYS_RESOLVE'] },
    evidence_package_aware: {
      status: 'pass',
      availability: 'available',
      package_schema_versions: ['1.0.0'],
      loader_diagnostics: {
        discovered: ['evidence_section_02.json'],
        loaded: [{ file: 'evidence_section_02.json', compatibility: 'canonical-v1' }],
        rejected: [],
      },
    },
  };
}

function report() {
  return generateCompatibilityReport(determinations(), { generatedAt: '2026-01-01T00:00:00.000Z' });
}

test('schema is a parseable Draft 2020-12 contract with the expected identity', () => {
  const schema = JSON.parse(readFileSync(new URL('../provenance/review_compatibility_report.schema.json', import.meta.url), 'utf8'));
  assert.equal(schema.$schema, 'https://json-schema.org/draft/2020-12/schema');
  assert.equal(schema.properties.schema_version.const, 1);
  assert.deepEqual(schema.$defs.status.enum, COMPATIBILITY_STATUSES);
});

test('generator adds deterministic contract metadata and validates its output', () => {
  const generated = report();
  assert.equal(generated.schema_version, 1);
  assert.equal(generated.generated_at, '2026-01-01T00:00:00.000Z');
  assert.deepEqual(validateCompatibilityReport(generated), { valid: true, errors: [] });
});

test('only the four documented compatibility statuses are accepted', () => {
  for (const status of COMPATIBILITY_STATUSES) {
    const candidate = report();
    candidate.article_renderable = {
      status,
      evidence: ['validator result'],
    };
    assert.equal(validateCompatibilityReport(candidate).valid, true, status);
  }
  const candidate = report();
  candidate.article_renderable.status = 'unknown';
  assert.equal(validateCompatibilityReport(candidate).valid, false);
});

test('conclusive article and citation statuses require validation evidence', () => {
  for (const field of ['article_renderable', 'citation_aware']) {
    for (const status of ['pass', 'fail']) {
      const candidate = report();
      candidate[field] = { status, evidence: [] };
      const result = validateCompatibilityReport(candidate);
      assert.equal(result.valid, false);
      assert(result.errors.some(error => error.includes(`${field}.evidence`)));
    }
  }
});

test('evidence-package pass requires availability, schema versions, successful diagnostics, and no rejection', () => {
  const mutations = [
    level => { level.availability = 'not_provided'; },
    level => { level.package_schema_versions = []; },
    level => { level.loader_diagnostics.discovered = []; },
    level => { level.loader_diagnostics.loaded = []; },
    level => { level.loader_diagnostics.rejected = [{ file: 'bad.json', reason: 'invalid package' }]; },
  ];
  for (const mutate of mutations) {
    const candidate = report();
    mutate(candidate.evidence_package_aware);
    assert.equal(validateCompatibilityReport(candidate).valid, false);
  }
});

test('not-provided evidence is an explicit, empty state', () => {
  const candidate = report();
  candidate.evidence_package_aware = {
    status: 'not_provided',
    availability: 'not_provided',
    package_schema_versions: [],
    loader_diagnostics: { discovered: [], loaded: [], rejected: [] },
  };
  assert.equal(validateCompatibilityReport(candidate).valid, true);
  candidate.evidence_package_aware.loader_diagnostics.discovered.push('unexpected.json');
  assert.equal(validateCompatibilityReport(candidate).valid, false);
});

test('claim graph is optional and its extension validation status remains descriptive', () => {
  const withoutClaimGraph = report();
  assert.equal(validateCompatibilityReport(withoutClaimGraph).valid, true);

  const withClaimGraph = report();
  withClaimGraph.claim_graph_aware = {
    status: 'pass',
    extensions: [{
      name: 'external-claim-graph',
      path: 'extensions/claim-graph.json',
      validation_status: 'upstream validator said: internally consistent',
    }],
  };
  assert.equal(validateCompatibilityReport(withClaimGraph).valid, true);

  withClaimGraph.claim_graph_aware.extensions = [];
  assert.equal(validateCompatibilityReport(withClaimGraph).valid, false);
});

test('generator refuses contradictory determinations', () => {
  const input = determinations();
  input.evidence_package_aware.loader_diagnostics.loaded = [];
  assert.throws(() => generateCompatibilityReport(input), /Invalid compatibility report/);
});

test('generator owns schema and timestamp metadata', () => {
  const input = {
    ...determinations(),
    schema_version: 999,
    generated_at: 'not a timestamp',
  };
  const generated = generateCompatibilityReport(input, { generatedAt: '2026-01-02T03:04:05.000Z' });
  assert.equal(generated.schema_version, 1);
  assert.equal(generated.generated_at, '2026-01-02T03:04:05.000Z');
});

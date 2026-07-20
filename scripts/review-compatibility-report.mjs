import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const COMPATIBILITY_REPORT_SCHEMA_VERSION = 1;
export const COMPATIBILITY_STATUSES = Object.freeze([
  'pass', 'fail', 'not_provided', 'not_applicable',
]);

const STATUS_SET = new Set(COMPATIBILITY_STATUSES);
const ISO_DATE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i;
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = value => typeof value === 'string' && value.trim().length > 0;

function rejectUnknown(value, allowed, path, errors) {
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) errors.push(`${path}.${key} is not allowed`);
  }
}

function requireKeys(value, keys, path, errors) {
  for (const key of keys) {
    if (!own(value, key)) errors.push(`${path}.${key} is required`);
  }
}

function validateStatus(value, path, errors) {
  if (!STATUS_SET.has(value)) errors.push(`${path} must be one of ${COMPATIBILITY_STATUSES.join(', ')}`);
}

function validateTextArray(value, path, errors, { unique = false } = {}) {
  if (!Array.isArray(value)) {
    errors.push(`${path} must be an array`);
    return;
  }
  value.forEach((item, index) => {
    if (!text(item)) errors.push(`${path}[${index}] must be a non-empty string`);
  });
  if (unique && new Set(value).size !== value.length) errors.push(`${path} must not contain duplicates`);
}

function validateEvidencedLevel(value, path, errors) {
  if (!object(value)) {
    errors.push(`${path} must be an object`);
    return;
  }
  requireKeys(value, ['status', 'evidence'], path, errors);
  rejectUnknown(value, ['status', 'evidence'], path, errors);
  validateStatus(value.status, `${path}.status`, errors);
  validateTextArray(value.evidence, `${path}.evidence`, errors, { unique: true });
  if (['pass', 'fail'].includes(value.status) && Array.isArray(value.evidence) && value.evidence.length === 0) {
    errors.push(`${path}.evidence must contain at least one validation result when status is ${value.status}`);
  }
}

function validateDiagnosticRecords(value, path, fields, errors) {
  if (!Array.isArray(value)) {
    errors.push(`${path} must be an array`);
    return;
  }
  value.forEach((record, index) => {
    const itemPath = `${path}[${index}]`;
    if (!object(record)) {
      errors.push(`${itemPath} must be an object`);
      return;
    }
    requireKeys(record, fields, itemPath, errors);
    rejectUnknown(record, fields, itemPath, errors);
    fields.forEach(field => {
      if (!text(record[field])) errors.push(`${itemPath}.${field} must be a non-empty string`);
    });
  });
}

function validateLoaderDiagnostics(value, path, errors) {
  if (!object(value)) {
    errors.push(`${path} must be an object`);
    return;
  }
  const fields = ['discovered', 'loaded', 'rejected'];
  requireKeys(value, fields, path, errors);
  rejectUnknown(value, fields, path, errors);
  validateTextArray(value.discovered, `${path}.discovered`, errors, { unique: true });
  validateDiagnosticRecords(value.loaded, `${path}.loaded`, ['file', 'compatibility'], errors);
  validateDiagnosticRecords(value.rejected, `${path}.rejected`, ['file', 'reason'], errors);
}

function validateEvidencePackageLevel(value, path, errors) {
  if (!object(value)) {
    errors.push(`${path} must be an object`);
    return;
  }
  const fields = ['status', 'availability', 'package_schema_versions', 'loader_diagnostics'];
  requireKeys(value, fields, path, errors);
  rejectUnknown(value, fields, path, errors);
  validateStatus(value.status, `${path}.status`, errors);
  if (!['available', 'not_provided'].includes(value.availability)) {
    errors.push(`${path}.availability must be available or not_provided`);
  }
  validateTextArray(value.package_schema_versions, `${path}.package_schema_versions`, errors, { unique: true });
  validateLoaderDiagnostics(value.loader_diagnostics, `${path}.loader_diagnostics`, errors);

  if (value.status === 'pass') {
    if (value.availability !== 'available') errors.push(`${path}.availability must be available when status is pass`);
    if (Array.isArray(value.package_schema_versions) && value.package_schema_versions.length === 0) {
      errors.push(`${path}.package_schema_versions must contain at least one version when status is pass`);
    }
    const diagnostics = value.loader_diagnostics;
    if (object(diagnostics)) {
      if (Array.isArray(diagnostics.discovered) && diagnostics.discovered.length === 0) errors.push(`${path}.loader_diagnostics.discovered must not be empty when status is pass`);
      if (Array.isArray(diagnostics.loaded) && diagnostics.loaded.length === 0) errors.push(`${path}.loader_diagnostics.loaded must not be empty when status is pass`);
      if (Array.isArray(diagnostics.rejected) && diagnostics.rejected.length > 0) errors.push(`${path}.loader_diagnostics.rejected must be empty when status is pass`);
    }
  }

  if (value.status === 'not_provided') {
    if (value.availability !== 'not_provided') errors.push(`${path}.availability must be not_provided when status is not_provided`);
    if (Array.isArray(value.package_schema_versions) && value.package_schema_versions.length > 0) errors.push(`${path}.package_schema_versions must be empty when status is not_provided`);
    const diagnostics = value.loader_diagnostics;
    if (object(diagnostics)) {
      for (const field of ['discovered', 'loaded', 'rejected']) {
        if (Array.isArray(diagnostics[field]) && diagnostics[field].length > 0) errors.push(`${path}.loader_diagnostics.${field} must be empty when status is not_provided`);
      }
    }
  }
}

function validateClaimGraphLevel(value, path, errors) {
  if (!object(value)) {
    errors.push(`${path} must be an object`);
    return;
  }
  requireKeys(value, ['status', 'extensions'], path, errors);
  rejectUnknown(value, ['status', 'extensions'], path, errors);
  validateStatus(value.status, `${path}.status`, errors);
  if (!Array.isArray(value.extensions)) {
    errors.push(`${path}.extensions must be an array`);
    return;
  }
  value.extensions.forEach((extension, index) => {
    const itemPath = `${path}.extensions[${index}]`;
    if (!object(extension)) {
      errors.push(`${itemPath} must be an object`);
      return;
    }
    const fields = ['name', 'path', 'validation_status'];
    requireKeys(extension, fields, itemPath, errors);
    rejectUnknown(extension, fields, itemPath, errors);
    fields.forEach(field => {
      if (!text(extension[field])) errors.push(`${itemPath}.${field} must be a non-empty string`);
    });
  });
  if (value.status === 'pass' && value.extensions.length === 0) {
    errors.push(`${path}.extensions must contain at least one extension when status is pass`);
  }
}

/** Validate the report without installing a JSON Schema runtime dependency. */
export function validateCompatibilityReport(report) {
  const errors = [];
  if (!object(report)) return { valid: false, errors: ['report must be an object'] };
  const required = ['schema_version', 'generated_at', 'validator_version', 'article_renderable', 'citation_aware', 'evidence_package_aware'];
  const allowed = [...required, 'claim_graph_aware'];
  requireKeys(report, required, 'report', errors);
  rejectUnknown(report, allowed, 'report', errors);
  if (report.schema_version !== COMPATIBILITY_REPORT_SCHEMA_VERSION) errors.push(`report.schema_version must be ${COMPATIBILITY_REPORT_SCHEMA_VERSION}`);
  if (!text(report.generated_at) || !ISO_DATE_TIME.test(report.generated_at) || Number.isNaN(Date.parse(report.generated_at))) errors.push('report.generated_at must be an ISO-8601 date-time string');
  if (!text(report.validator_version)) errors.push('report.validator_version must be a non-empty string');
  validateEvidencedLevel(report.article_renderable, 'report.article_renderable', errors);
  validateEvidencedLevel(report.citation_aware, 'report.citation_aware', errors);
  validateEvidencePackageLevel(report.evidence_package_aware, 'report.evidence_package_aware', errors);
  if (own(report, 'claim_graph_aware')) validateClaimGraphLevel(report.claim_graph_aware, 'report.claim_graph_aware', errors);
  return { valid: errors.length === 0, errors };
}

/** Add contract metadata to phase determinations, then validate the result. */
export function generateCompatibilityReport(determinations, { generatedAt = new Date().toISOString() } = {}) {
  if (!object(determinations)) throw new TypeError('determinations must be an object');
  const report = {
    ...determinations,
    schema_version: COMPATIBILITY_REPORT_SCHEMA_VERSION,
    generated_at: generatedAt,
  };
  const result = validateCompatibilityReport(report);
  if (!result.valid) throw new Error(`Invalid compatibility report:\n- ${result.errors.join('\n- ')}`);
  return report;
}

function usage() {
  return 'Usage:\n  node scripts/review-compatibility-report.mjs validate [report.json]\n  node scripts/review-compatibility-report.mjs generate <determinations.json> [report.json]';
}

function runCli(argv) {
  const [command, input, output] = argv;
  if (command === 'validate') {
    const path = resolve(input || 'provenance/review_compatibility_report.json');
    const result = validateCompatibilityReport(JSON.parse(readFileSync(path, 'utf8')));
    if (!result.valid) throw new Error(`Invalid compatibility report ${path}:\n- ${result.errors.join('\n- ')}`);
    process.stdout.write(`Valid compatibility report: ${path}\n`);
    return;
  }
  if (command === 'generate' && input) {
    const target = resolve(output || 'provenance/review_compatibility_report.json');
    const report = generateCompatibilityReport(JSON.parse(readFileSync(resolve(input), 'utf8')));
    writeFileSync(target, `${JSON.stringify(report, null, 2)}\n`);
    process.stdout.write(`Wrote compatibility report: ${target}\n`);
    return;
  }
  throw new Error(usage());
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : '';
if (invokedPath === fileURLToPath(import.meta.url)) {
  try {
    runCli(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

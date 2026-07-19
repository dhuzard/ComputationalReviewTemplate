// evidence-explorer-plugin.mjs
//
// The explorer consumes the versioned contract in evidence/schema. Older
// per-section packages are adapted at this single boundary; the widget never
// needs to guess which historical producer created a package.
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { resolve, dirname, basename } from 'node:path';

export const EVIDENCE_SCHEMA_VERSION = '1.0.0';
const CANONICAL_FILENAME = /^evidence_section_(\d+)\.json$/;
const LEGACY_FILENAMES = [
  /^section_(\d+)_evidence_package\.json$/,
  /^section_(\d+)_evidence\.json$/,
];

const evidenceDirective = {
  name: 'evidence-explorer',
  doc: 'Interactive evidence database explorer. Packages are discovered from evidence/manifest.json or documented evidence filename patterns.',
  options: { 'evidence-dir': { type: String }, availability: { type: String }, height: { type: String } },
  run(data) {
    return [{
      type: 'evidence-explorer',
      evidenceDir: data.options?.['evidence-dir'] || '../evidence',
      evidenceAvailability: data.options?.availability || 'available',
      height: data.options?.height || '700px',
    }];
  },
};

function asArray(value) { return Array.isArray(value) ? value : []; }
function integer(value) { return Number.isInteger(value) && value >= 0; }
function text(value) { return typeof value === 'string' && value.trim().length > 0; }

/**
 * Validate the current contract without adding a runtime JSON-schema package.
 * The normative, machine-readable definition is evidence/schema/package.schema.json.
 */
export function validateCanonicalPackage(value) {
  const errors = [];
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ['package must be an object'];
  if (value.evidence_package_schema_version !== EVIDENCE_SCHEMA_VERSION) {
    errors.push(`evidence_package_schema_version must be ${EVIDENCE_SCHEMA_VERSION}`);
  }
  if (!value.section || typeof value.section !== 'object') errors.push('section is required');
  else {
    if (!integer(value.section.order)) errors.push('section.order must be a non-negative integer');
    if (!text(value.section.id)) errors.push('section.id must be a non-empty string');
    if (!text(value.section.title)) errors.push('section.title must be a non-empty string');
  }
  for (const field of ['findings', 'conflicts', 'figure_data', 'evidence_gaps']) {
    if (!Array.isArray(value[field])) errors.push(`${field} must be an array`);
  }
  if (!value.provenance || typeof value.provenance !== 'object') errors.push('provenance is required');
  for (const [index, finding] of asArray(value.findings).entries()) {
    if (!finding || typeof finding !== 'object' || Array.isArray(finding)) { errors.push(`findings[${index}] must be an object`); continue; }
    if (!text(finding.claim)) errors.push(`findings[${index}].claim must be a non-empty string`);
    if (!asArray(finding.sources).length) errors.push(`findings[${index}].sources must contain a source`);
    for (const [sourceIndex, source] of asArray(finding.sources).entries()) {
      if (!source || typeof source !== 'object' || !text(source.source_id)) errors.push(`findings[${index}].sources[${sourceIndex}].source_id is required`);
    }
  }
  return errors;
}

function legacyFinding(finding, index) {
  if (!finding || typeof finding !== 'object' || Array.isArray(finding)) {
    throw new Error(`legacy findings[${index}] must be an object`);
  }
  const sourceId = finding.cite_key || finding.doi;
  if (!text(finding.claim) || !text(sourceId)) {
    throw new Error(`legacy findings[${index}] requires claim and cite_key or doi`);
  }
  const source = { source_id: sourceId };
  if (text(finding.doi)) source.doi = finding.doi;
  const passage = finding.claim_source_sentence || finding.supporting_passage;
  if (text(passage)) source.supporting_passages = [{ text: passage, locator: finding.locator || undefined }];
  return { ...finding, sources: [source] };
}

/** Adapt the two historical filenames/shapes explicitly supported by v1. */
export function adaptEvidencePackage(raw, { filename = 'package.json' } = {}) {
  if (raw?.evidence_package_schema_version === EVIDENCE_SCHEMA_VERSION) {
    const errors = validateCanonicalPackage(raw);
    if (errors.length) throw new Error(errors.join('; '));
    return { package: raw, compatibility: 'canonical-v1' };
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('legacy package must be an object');
  const match = filename.match(/(\d+)/);
  const order = Number(raw.section_id ?? match?.[1]);
  if (!integer(order)) throw new Error('legacy package requires numeric section_id or a section filename');
  const findings = asArray(raw.findings).map(legacyFinding);
  const section = { id: String(raw.section_id ?? order), order, title: raw.section_title || `Section ${order}` };
  const packageV1 = {
    evidence_package_schema_version: EVIDENCE_SCHEMA_VERSION,
    section,
    findings,
    conflicts: asArray(raw.conflicts),
    figure_data: asArray(raw.figure_data),
    evidence_gaps: asArray(raw.evidence_gaps),
    replication: raw.replication || { unreplicated_claims: asArray(raw.unreplicated_claims) },
    provenance: raw.provenance || { migrated_from: filename, source_schema: 'legacy-section-package' },
  };
  const errors = validateCanonicalPackage(packageV1);
  if (errors.length) throw new Error(errors.join('; '));
  return { package: packageV1, compatibility: 'legacy-section-package' };
}

function patternEntry(filename) {
  const canonical = filename.match(CANONICAL_FILENAME);
  if (canonical) return { file: filename, order: Number(canonical[1]), compatibility: 'canonical-v1' };
  for (const pattern of LEGACY_FILENAMES) {
    const legacy = filename.match(pattern);
    if (legacy) return { file: filename, order: Number(legacy[1]), compatibility: 'legacy-section-package' };
  }
  return null;
}

export function discoverEvidenceFiles(evidenceDir) {
  if (!existsSync(evidenceDir)) return { mode: 'none', entries: [], error: 'evidence directory does not exist' };
  const manifestPath = resolve(evidenceDir, 'manifest.json');
  if (existsSync(manifestPath)) {
    try {
      const manifest = JSON.parse(readFileSync(manifestPath, 'utf-8'));
      if (manifest?.schema_version !== 1 || !Array.isArray(manifest.packages)) throw new Error('manifest requires schema_version: 1 and packages[]');
      const entries = manifest.packages.map((entry, index) => {
        if (!text(entry?.file) || entry.file !== basename(entry.file) || !entry.file.endsWith('.json') || !integer(entry?.order)) {
          throw new Error(`manifest.packages[${index}] requires a local JSON file and non-negative order`);
        }
        return { file: entry.file, order: entry.order, compatibility: 'manifest' };
      });
      return { mode: 'manifest', entries: entries.sort((a, b) => a.order - b.order || a.file.localeCompare(b.file)) };
    } catch (error) { return { mode: 'manifest-invalid', entries: [], error: error.message }; }
  }
  const entries = readdirSync(evidenceDir, { withFileTypes: true })
    .filter(entry => entry.isFile())
    .map(entry => patternEntry(entry.name))
    .filter(Boolean)
    .sort((a, b) => a.order - b.order || a.file.localeCompare(b.file));
  return { mode: 'pattern', entries };
}

function diagnosticStatus({ directory, discovered, loaded, rejected }) {
  if (directory.mode === 'none') return 'missing_directory';
  if (directory.mode === 'manifest-invalid') return 'invalid_manifest';
  if (!discovered.length) return 'no_compatible_files';
  if (!loaded.length) return 'invalid_packages';
  if (rejected.length) return 'partial_loading';
  if (loaded.every(entry => entry.package.findings.length === 0)) return 'loaded_zero_findings';
  return 'loaded';
}

export function loadEvidenceDirectory(evidenceDir) {
  const discovery = discoverEvidenceFiles(evidenceDir);
  const loaded = [];
  const rejected = [];
  for (const entry of discovery.entries) {
    const path = resolve(evidenceDir, entry.file);
    try {
      const raw = JSON.parse(readFileSync(path, 'utf-8'));
      const adapted = adaptEvidencePackage(raw, { filename: basename(path) });
      loaded.push({ ...entry, ...adapted });
    } catch (error) { rejected.push({ file: entry.file, reason: error.message }); }
  }
  const status = diagnosticStatus({ directory: discovery, discovered: discovery.entries, loaded, rejected });
  return {
    status,
    discovery_mode: discovery.mode,
    diagnostics: {
      message: discovery.error || null,
      discovered: discovery.entries.map(entry => entry.file),
      loaded: loaded.map(entry => ({ file: entry.file, compatibility: entry.compatibility })),
      rejected,
    },
    packages: loaded.map(entry => entry.package),
  };
}

const BLOCKING_AVAILABLE_STATUSES = new Set([
  'missing_directory', 'invalid_manifest', 'no_compatible_files', 'invalid_packages',
]);

/** Apply the page author's explicit declaration to a loader result. */
export function applyEvidenceAvailability(result, availability = 'available') {
  if (!['available', 'not_provided'].includes(availability)) {
    throw new Error(`evidence-explorer :availability: must be available or not_provided (received ${availability})`);
  }
  if (availability === 'not_provided') {
    return {
      ...result,
      status: 'not_provided',
      packages: [],
      diagnostics: { ...result.diagnostics, message: 'This review explicitly declares that no Evidence Database is provided.' },
    };
  }
  if (BLOCKING_AVAILABLE_STATUSES.has(result.status)) {
    const detail = result.diagnostics.message || result.diagnostics.rejected.map(entry => `${entry.file}: ${entry.reason}`).join('; ');
    throw new Error(`Evidence Database is declared available but no compatible package could be loaded (${result.status}). ${detail}`);
  }
  return result;
}

function normalizeConflict(conflict, section) {
  return {
    ...conflict,
    section,
    topic: conflict.topic || conflict.description || '',
    nature_of_conflict: conflict.nature_of_conflict || conflict.description || conflict.topic || '',
    side_a: conflict.paper_a_claim || conflict.paper1_claim || conflict.claim_a || conflict.side_a || '',
    side_b: conflict.paper_b_claim || conflict.paper2_claim || conflict.claim_b || conflict.side_b || '',
    paper_a_doi: conflict.paper_a_doi || conflict.paper1_doi || '',
    paper_b_doi: conflict.paper_b_doi || conflict.paper2_doi || '',
  };
}

export function explorerDataFromLoad(result) {
  const sections = [];
  const findings = [];
  const conflicts = [];
  const figureData = [];
  for (const pkg of result.packages) {
    const section = pkg.section.order;
    const packageFindings = pkg.findings.map(finding => {
      const primarySource = finding.sources[0] || {};
      return {
        ...finding,
        cite_key: finding.cite_key || primarySource.source_id,
        doi: finding.doi || primarySource.doi || '',
        claim_source_sentence: finding.claim_source_sentence || primarySource.supporting_passages?.[0]?.text || '',
        section,
        section_title: pkg.section.title,
        tier: finding.tier || finding.replication_status,
      };
    });
    const packageConflicts = pkg.conflicts.map(conflict => normalizeConflict(conflict, section));
    const packageFigureData = pkg.figure_data.map(data => ({ ...data, section }));
    sections.push({ section, title: pkg.section.title, papers: new Set(packageFindings.flatMap(f => f.sources.map(s => s.doi || s.source_id))).size, findings: packageFindings.length, conflicts: packageConflicts.length, figure_comparisons: packageFigureData.length });
    findings.push(...packageFindings); conflicts.push(...packageConflicts); figureData.push(...packageFigureData);
  }
  return { sections, findings, conflicts, figure_data: figureData, evidence_status: result.status, diagnostics: result.diagnostics };
}

const evidenceTransform = {
  name: 'evidence-data-loader',
  stage: 'document',
  plugin: () => (tree, vfile) => {
    function transform(node) {
      if (node?.type === 'evidence-explorer') {
        const documentDir = vfile?.path ? dirname(vfile.path) : process.cwd();
        const loaded = loadEvidenceDirectory(resolve(documentDir, node.evidenceDir || '../evidence'));
        const result = applyEvidenceAvailability(loaded, node.evidenceAvailability || 'available');
        node.type = 'anywidget';
        node.id = `evidence-explorer-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
        node.esm = './evidence-explorer-widget.mjs';
        node.model = { evidence_data: JSON.stringify(explorerDataFromLoad(result)), height: node.height || '700px' };
      }
      for (const child of node?.children || []) transform(child);
    }
    transform(tree);
  },
};

export default { name: 'Evidence Explorer Plugin', directives: [evidenceDirective], transforms: [evidenceTransform] };

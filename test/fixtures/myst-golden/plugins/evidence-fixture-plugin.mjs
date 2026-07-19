function walk(node) {
  if (!node) return;
  if (node.type === 'evidence-explorer') {
    node.type = 'paragraph';
    node.children = [{ type: 'text', value: 'Evidence Explorer fixture rendered.' }];
  }
  for (const child of node.children || []) walk(child);
}

export default {
  name: 'Evidence fixture',
  directives: [{ name: 'evidence-explorer', options: { 'evidence-dir': { type: String }, availability: { type: String } }, run: () => [{ type: 'evidence-explorer' }] }],
  transforms: [{ name: 'evidence-fixture-transform', stage: 'document', plugin: () => tree => walk(tree) }],
};

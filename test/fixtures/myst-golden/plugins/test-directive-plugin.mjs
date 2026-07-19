function walk(node) {
  if (!node) return;
  if (node.type === 'test-note') {
    node.type = 'paragraph';
    node.children = [{ type: 'text', value: 'Custom directive fixture rendered.' }];
  }
  for (const child of node.children || []) walk(child);
}

export default {
  name: 'Test directive fixture',
  directives: [{ name: 'test-note', run: () => [{ type: 'test-note' }] }],
  transforms: [{ name: 'test-directive-transform', stage: 'document', plugin: () => tree => walk(tree) }],
};

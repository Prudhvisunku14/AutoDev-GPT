export interface TreeNode {
  name: string;
  path: string;
  children: TreeNode[];
  file: boolean;
}

/** Slash-separated paths as a tree: folders first, then files, each alphabetical. */
export function buildTree(paths: string[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", children: [], file: false };
  for (const path of [...paths].sort()) {
    let node = root;
    path.split("/").forEach((part, index, parts) => {
      const file = index === parts.length - 1;
      let child = node.children.find((c) => c.name === part && c.file === file);
      if (!child) {
        child = { name: part, path: parts.slice(0, index + 1).join("/"), children: [], file };
        node.children.push(child);
      }
      node = child;
    });
  }
  const sort = (nodes: TreeNode[]): TreeNode[] =>
    nodes.sort((a, b) => Number(a.file) - Number(b.file) || a.name.localeCompare(b.name)).map((n) => ({ ...n, children: sort(n.children) }));
  return sort(root.children);
}

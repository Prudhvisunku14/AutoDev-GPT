import { ChevronDown, ChevronRight, FileCode2, Folder, Loader2 } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "../lib/cn";
import { buildTree, type TreeNode } from "../lib/fileTree";
import { useFileContent } from "../lib/queries";

function Branch({ nodes, depth, selected, onSelect }: { nodes: TreeNode[]; depth: number; selected: string | null; onSelect: (path: string) => void }) {
  const [closed, setClosed] = useState<Set<string>>(new Set());
  return (
    <ul role={depth === 0 ? "tree" : "group"} aria-label={depth === 0 ? "Project files" : undefined}>
      {nodes.map((node) => {
        const open = !closed.has(node.path);
        return (
          <li key={node.path} role="treeitem" aria-expanded={node.file ? undefined : open} aria-selected={node.file ? selected === node.path : undefined}>
            <button
              type="button"
              onClick={() => (node.file ? onSelect(node.path) : setClosed((s) => (s.has(node.path) ? new Set([...s].filter((p) => p !== node.path)) : new Set(s).add(node.path))))}
              className={cn("flex w-full items-center gap-1.5 rounded-lg py-1 pr-2 text-left text-[13px] transition hover:bg-violet-50", selected === node.path && "bg-violet-50 font-medium text-violet-800")}
              style={{ paddingLeft: `${depth * 14 + 6}px` }}
            >
              {node.file ? (
                <FileCode2 className="size-3.5 shrink-0 text-slate-400" aria-hidden />
              ) : (
                <>
                  {open ? <ChevronDown className="size-3.5 shrink-0 text-slate-400" aria-hidden /> : <ChevronRight className="size-3.5 shrink-0 text-slate-400" aria-hidden />}
                  <Folder className="size-3.5 shrink-0 text-violet-400" aria-hidden />
                </>
              )}
              <span className="truncate">{node.name}</span>
            </button>
            {!node.file && open && node.children.length > 0 && <Branch nodes={node.children} depth={depth + 1} selected={selected} onSelect={onSelect} />}
          </li>
        );
      })}
    </ul>
  );
}

/** The project's files on the backend, with a read-only viewer. Content is shown as text, never rendered. */
export function FileBrowser({ projectId, paths }: { projectId: string; paths: string[] }) {
  const tree = useMemo(() => buildTree(paths), [paths]);
  // A selection belongs to one project and one existing file: switching projects, or the file disappearing, drops it.
  const [choice, setChoice] = useState<{ projectId: string; path: string } | null>(null);
  const selected = choice && choice.projectId === projectId && paths.includes(choice.path) ? choice.path : null;
  const setSelected = (path: string) => setChoice({ projectId, path });
  const content = useFileContent(projectId, selected);

  if (!paths.length) return <p className="text-[13px] text-muted">No files in the project yet. Files appear here once a verified build delivers them.</p>;

  return (
    <div className="space-y-3">
      <div className="relative max-h-72 overflow-auto rounded-xl border border-line bg-slate-50/60 p-1.5">
        <Branch nodes={tree} depth={0} selected={selected} onSelect={setSelected} />
      </div>
      {selected && (
        <div className="overflow-hidden rounded-xl border border-line bg-white">
          <p className="truncate border-b border-line bg-slate-50 px-3 py-1.5 font-mono text-[12px] text-muted">{selected}</p>
          {content.isLoading ? (
            <p className="flex items-center gap-2 p-3 text-[13px] text-muted">
              <Loader2 className="size-4 animate-spin" aria-hidden /> Loading…
            </p>
          ) : content.isError ? (
            <p className="p-3 text-[13px] text-rose-600">This file could not be shown (it may not be text).</p>
          ) : (
            <pre className="max-h-96 overflow-auto p-3 font-mono text-[12px] leading-relaxed">{content.data?.content}</pre>
          )}
        </div>
      )}
    </div>
  );
}

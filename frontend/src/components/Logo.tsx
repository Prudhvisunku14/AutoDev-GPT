import { Zap } from "lucide-react";

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span className="flex items-center gap-2.5">
      <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-blue-500 via-indigo-500 to-violet-600 text-white shadow-[0_6px_16px_-6px_rgb(99_102_241/0.7)]">
        <Zap className="size-5" fill="currentColor" aria-hidden />
      </span>
      {!compact && (
        <span className="leading-tight">
          <span className="block text-[17px] font-bold tracking-tight">
            DevPilot <span className="text-violet-600">AI</span>
          </span>
          <span className="block text-xs text-muted">AI Application Builder</span>
        </span>
      )}
    </span>
  );
}

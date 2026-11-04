import { AlertTriangle, Info, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../lib/cn";

const STYLES = {
  info: { box: "border-indigo-100 bg-indigo-50/60 text-indigo-900", icon: Info, iconClass: "text-indigo-500" },
  warn: { box: "border-amber-200 bg-amber-50/70 text-amber-900", icon: AlertTriangle, iconClass: "text-amber-500" },
  error: { box: "border-rose-200 bg-rose-50/70 text-rose-900", icon: XCircle, iconClass: "text-rose-500" },
} as const;

export function Notice({ tone = "info", title, children, className }: { tone?: keyof typeof STYLES; title?: string; children?: ReactNode; className?: string }) {
  const style = STYLES[tone];
  const Icon = style.icon;
  return (
    <div role={tone === "error" ? "alert" : "note"} className={cn("flex gap-3 rounded-xl border px-4 py-3 text-sm", style.box, className)}>
      <Icon className={cn("mt-0.5 size-4 shrink-0", style.iconClass)} aria-hidden />
      <div className="min-w-0">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={cn(title && "mt-0.5", "text-[13px] leading-relaxed opacity-90")}>{children}</div>}
      </div>
    </div>
  );
}

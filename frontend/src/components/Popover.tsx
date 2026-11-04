import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { cn } from "../lib/cn";

/** A button that opens a small panel. Escape and a click outside close it; focus returns to the button. */
export function Popover({
  label,
  button,
  children,
  buttonClassName,
  panelClassName,
}: {
  label: string;
  button: ReactNode;
  children: ReactNode;
  buttonClassName?: string;
  panelClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button ref={trigger} type="button" aria-label={label} aria-expanded={open} aria-controls={panelId} onClick={() => setOpen((v) => !v)} className={buttonClassName}>
        {button}
      </button>
      {open && (
        <div
          id={panelId}
          role="dialog"
          aria-label={label}
          // Anchored under its button from the sm breakpoint. On a phone the button is not at the screen's edge, so a panel
          // hung from it would leave the screen: there it spans the screen below the header instead, and scrolls if tall.
          className={cn(
            "absolute right-0 z-40 mt-2 w-80 rounded-2xl border border-line bg-white p-4 text-sm [overflow-wrap:anywhere] shadow-soft",
            panelClassName,
            "max-sm:fixed max-sm:inset-x-3 max-sm:top-16 max-sm:mt-0 max-sm:w-auto max-sm:max-h-[calc(100dvh-5rem)] max-sm:overflow-y-auto",
          )}
        >
          {children}
        </div>
      )}
    </div>
  );
}

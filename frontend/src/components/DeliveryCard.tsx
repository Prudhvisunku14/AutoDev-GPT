import { CheckCircle2, Download, Loader2, ShieldAlert, XCircle } from "lucide-react";
import { useState } from "react";
import { cn } from "../lib/cn";
import { deliveryVerdict } from "../lib/delivery";
import { formatCost } from "../lib/format";
import { useApi } from "../lib/apiContext";
import { useArtifact } from "../lib/queries";
import type { Report, Run } from "../lib/types";

/** The outcome of a finished run, worded only as strongly as the backend's record allows. */
export function DeliveryCard({ run, report, projectName }: { run: Run; report: Report | null; projectName: string }) {
  const verdict = deliveryVerdict(run, report);
  const artifact = useArtifact(run);
  const api = useApi();
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  if (verdict.kind === "in-progress") return null;
  const lastTest = report?.verifications?.at(-1);
  const acceptance = report?.acceptance;
  const style =
    verdict.kind === "success"
      ? { box: "border-emerald-200 bg-emerald-50/60", icon: CheckCircle2, tone: "text-emerald-600" }
      : verdict.kind === "unverified"
        ? { box: "border-amber-200 bg-amber-50/60", icon: ShieldAlert, tone: "text-amber-600" }
        : { box: "border-rose-200 bg-rose-50/60", icon: XCircle, tone: "text-rose-600" };
  const Icon = style.icon;

  async function download() {
    const filename = artifact.data?.filename;
    if (!artifact.data?.available || !filename || downloading) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      const file = await api.downloadArtifact(run.id);
      const url = URL.createObjectURL(file);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (failure) {
      setDownloadError(failure instanceof Error ? failure.message : "The artifact could not be downloaded.");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <section aria-labelledby="delivery-title" className={cn("rounded-2xl border p-5", style.box)} data-testid="delivery" data-verdict={verdict.kind}>
      <h2 id="delivery-title" className="flex items-center gap-2 text-base font-semibold">
        <Icon className={cn("size-5", style.tone)} aria-hidden />
        {verdict.title}
      </h2>
      {"reason" in verdict && <p className="mt-1.5 text-sm text-slate-700">{verdict.reason}</p>}
      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-muted">Project</dt>
          <dd className="font-medium">{projectName}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Files delivered</dt>
          <dd className="font-medium">{report ? report.delivered_files.length : "—"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Engine's own tests</dt>
          <dd className="font-medium">{lastTest ? `${lastTest.passed} of ${lastTest.ran} passed` : "Not run"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Independent acceptance</dt>
          <dd className="font-medium">{acceptance?.configured ? `${acceptance.verdict ?? "—"} (${acceptance.passed ?? 0} of ${acceptance.total ?? 0})` : "Not configured"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Recorded cost</dt>
          <dd className="font-medium">{formatCost(run)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Simulated parts</dt>
          <dd className="font-medium">{report ? (report.provenance.simulated.length ? report.provenance.simulated.join(", ") : "None") : "—"}</dd>
        </div>
      </dl>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={!artifact.data?.available || downloading}
          onClick={() => void download()}
          className="inline-flex items-center gap-2 rounded-xl border border-line bg-white/70 px-3.5 py-2 text-sm font-medium enabled:hover:bg-white disabled:cursor-not-allowed disabled:text-slate-400"
          aria-describedby="download-note"
        >
          {downloading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Download className="size-4" aria-hidden />}
          Download project
        </button>
        <p id="download-note" className="text-xs text-muted">
          {artifact.isLoading
            ? "Checking for a verified project archive…"
            : artifact.isError
              ? "The backend could not confirm whether a verified archive is available."
              : artifact.data?.available
                ? `${artifact.data.verification === "generated-tests" ? "Verified by the generated tests only; no independent acceptance suite judged it. " : ""}${artifact.data.files.length} files and the verification report; secrets and temporary files are excluded.`
                : artifact.data?.reason ?? "A ZIP is offered only after independent verification passes."}
        </p>
        {downloadError && <p role="alert" className="w-full text-xs text-rose-600">{downloadError}</p>}
        {artifact.data?.simulated?.length ? <p className="w-full text-xs text-amber-700">Recorded simulated components: {artifact.data.simulated.join(", ")}.</p> : null}
      </div>
    </section>
  );
}

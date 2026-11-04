import { Download, History, Loader2 } from "lucide-react";
import { useState } from "react";
import { useApi } from "../lib/apiContext";
import { formatDateTime } from "../lib/format";
import { useHistoryArtifact, useHistoryRun, useHistoryRuns } from "../lib/queries";
import { Notice } from "./Notice";

function ArchiveButton({ recordId, filename, onError }: { recordId: string; filename: string; onError: (message: string | null) => void }) {
  const api = useApi();
  const [busy, setBusy] = useState(false);

  async function download() {
    if (busy) return;
    setBusy(true);
    onError(null);
    try {
      const file = await api.downloadHistoryArtifact(recordId);
      const url = URL.createObjectURL(file);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (failure) {
      onError(failure instanceof Error ? failure.message : "The historical archive could not be downloaded.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <button type="button" onClick={() => void download()} disabled={busy} className="inline-flex items-center gap-2 rounded-lg border border-line px-3 py-1.5 text-xs font-medium disabled:opacity-60">
      {busy ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Download className="size-3.5" aria-hidden />}
      Download verified archive
    </button>
  );
}

export function HistoricalRuns() {
  const history = useHistoryRuns();
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const selected = useHistoryRun(selectedId);
  const artifact = useHistoryArtifact(selectedId);

  if (history.isLoading) return <p className="mt-8 flex items-center gap-2 text-sm text-muted"><Loader2 className="size-4 animate-spin" aria-hidden /> Checking terminal build history…</p>;
  if (history.isError) {
    return <Notice tone="error" title="Historical builds could not be loaded" className="mt-8">{history.error instanceof Error ? history.error.message : "Unknown backend error."}</Notice>;
  }
  if (!history.data?.length) return null;

  return (
    <section className="mt-10" aria-labelledby="historical-runs-title">
      <h2 id="historical-runs-title" className="flex items-center gap-2 text-lg font-semibold">
        <History className="size-5 text-violet-600" aria-hidden />
        Terminal build history
      </h2>
      <p className="mt-1 text-sm text-muted">Read-only records from earlier terminal runs. These are historical and are not new dashboard builds.</p>
      <ul className="mt-4 divide-y divide-line overflow-hidden rounded-2xl border border-line bg-white shadow-soft">
        {history.data.map((record) => (
          <li key={record.id} className="p-4 sm:px-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="font-medium">{record.project_name ?? "Historical project"}</p>
                <p className="mt-1 text-xs text-muted">
                  Historical · {formatDateTime(record.recorded_at)} · {record.state ?? record.outcome ?? "Recorded"}
                  {record.cost_usd !== null ? ` · $${record.cost_usd.toFixed(4)}` : ""}
                </p>
              </div>
              <button
                type="button"
                aria-expanded={selectedId === record.id}
                onClick={() => { setSelectedId((current) => current === record.id ? undefined : record.id); setDownloadError(null); }}
                className="rounded-lg border border-line px-3 py-1.5 text-xs font-medium hover:bg-slate-50"
              >
                {selectedId === record.id ? "Hide evidence" : "View evidence"}
              </button>
            </div>
            {selectedId === record.id && (
              <div className="mt-4 rounded-xl bg-slate-50 p-4">
                {selected.isLoading ? <p className="text-sm text-muted">Loading the recorded report…</p> : selected.isError ? (
                  <p className="text-sm text-rose-600">{selected.error instanceof Error ? selected.error.message : "The historical report could not be loaded."}</p>
                ) : selected.data ? (
                  <>
                    <p className="text-xs text-muted">{selected.data.source}. Run ID: {selected.data.run_id ?? "not recorded"}.</p>
                    <p className="mt-2 text-sm">{selected.data.detail ?? selected.data.outcome ?? "No outcome detail was recorded."}</p>
                    {selected.data.requirement && <p className="mt-2 text-sm text-slate-700">{selected.data.requirement}</p>}
                    {selected.data.report && (
                      <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                        <div><dt className="text-muted">Independent acceptance</dt><dd className="font-medium">{selected.data.report.acceptance.configured ? `${selected.data.report.acceptance.verdict ?? "unknown"} (${selected.data.report.acceptance.passed ?? 0}/${selected.data.report.acceptance.total ?? 0})` : "Not configured"}</dd></div>
                        <div><dt className="text-muted">Test runs</dt><dd className="font-medium">{selected.data.report.verifications.map((item) => `${item.verdict} (${item.passed}/${item.ran})`).join("; ") || "None recorded"}</dd></div>
                        <div><dt className="text-muted">Delivered files</dt><dd className="font-medium">{selected.data.delivered.join(", ") || "None recorded"}</dd></div>
                        <div><dt className="text-muted">Simulated components</dt><dd className="font-medium">{selected.data.report.provenance.simulated.join(", ") || "None recorded"}</dd></div>
                      </dl>
                    )}
                    <div className="mt-3">
                      {artifact.data?.available && artifact.data.filename ? (
                        <ArchiveButton recordId={record.id} filename={artifact.data.filename} onError={setDownloadError} />
                      ) : <p className="text-xs text-muted">{artifact.data?.reason ?? "No verified archive is available for this record."}</p>}
                      {downloadError && <p role="alert" className="mt-2 text-xs text-rose-600">{downloadError}</p>}
                    </div>
                  </>
                ) : null}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

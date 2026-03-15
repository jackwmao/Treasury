import { useEffect, useMemo, useState } from "react";
import { ReviewPanel } from "./components/ReviewPanel";
import {
  BatchResultItem,
  ComplianceFinding,
  PdfPickerResponse,
  PdfTableDebugResponse,
  VerificationResult,
  debugPdfPicker,
  debugPdfTable
} from "./lib/api";

function normalizeStatus(status: string): string {
  return status === "fail" ? "needs_review" : status;
}

function StatusBadge({ status }: { status: string }) {
  const normalizedStatus = normalizeStatus(status);
  const cls = normalizedStatus === "pass" ? "ok" : "warn";
  const label = normalizedStatus === "needs_review" ? "NEEDS REVIEW" : normalizedStatus.toUpperCase();
  return <span className={`badge ${cls}`}>{label}</span>;
}

const SECTION_LABELS: Record<string, string> = {
  application_match: "Application match",
  regulatory_label_presence: "Regulatory label",
  label_review: "Summary"
};

function FindingItem({ finding }: { finding: ComplianceFinding }) {
  const shouldStripStatusSuffix =
    finding.cfr_section === "application_match" || finding.cfr_section === "regulatory_label_presence";
  const message =
    shouldStripStatusSuffix
      ? finding.message.replace(/:\s*(pass|fail|needs[_\s]review)\s*$/i, "")
      : finding.message;

  return (
    <li className="finding">
      <StatusBadge status={finding.status} />
      <span>{message}</span>
    </li>
  );
}

function groupFindingsBySection(findings: ComplianceFinding[]) {
  const groups: Record<string, ComplianceFinding[]> = {};
  for (const f of findings) {
    const section = f.cfr_section in SECTION_LABELS ? f.cfr_section : "other";
    if (!groups[section]) groups[section] = [];
    groups[section].push(f);
  }
  return groups;
}

function ValueRow({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="kv-row">
      <span className="kv-label">{label}</span>
      <span className="kv-value">{value && value.trim() ? value : "Not extracted"}</span>
    </div>
  );
}

function formatGovernmentWarning(value: string | null | undefined): string {
  if (!value?.trim()) return "Not found";
  const match = value.match(/(government warning\b[\s\S]*)/i);
  return match?.[1]?.trim() || "Not found";
}

export default function App() {
  const [isDebugPage, setIsDebugPage] = useState(() =>
    typeof window !== "undefined" ? window.location.hash === "#debug" : false
  );
  const [singleResult, setSingleResult] = useState<VerificationResult | null>(null);
  const [batchResults, setBatchResults] = useState<BatchResultItem[]>([]);
  const [runTotalCount, setRunTotalCount] = useState(0);
  const [selectedFilename, setSelectedFilename] = useState<string | null>(null);
  const [debugPdf, setDebugPdf] = useState<File | null>(null);
  const [debugResult, setDebugResult] = useState<PdfTableDebugResponse | null>(null);
  const [debugLoading, setDebugLoading] = useState(false);
  const [debugError, setDebugError] = useState<string | null>(null);

  const [pickerPdf, setPickerPdf] = useState<File | null>(null);
  const [pickerResult, setPickerResult] = useState<PdfPickerResponse | null>(null);
  const [pickerLoading, setPickerLoading] = useState(false);
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [pickerCoords, setPickerCoords] = useState<{ x: number; y: number } | null>(null);
  const [pickerPoints, setPickerPoints] = useState<Array<{ x: number; y: number }>>([]);

  const batchSummary = useMemo(() => {
    const completed = batchResults.length;
    const pass = batchResults.filter((r) => normalizeStatus(r.result.status) === "pass").length;
    const review = batchResults.filter((r) => normalizeStatus(r.result.status) === "needs_review").length;
    return { completed, pass, review };
  }, [batchResults]);

  const overallMultiStatus = useMemo(() => {
    if (runTotalCount <= 1 || batchSummary.completed !== runTotalCount) return null;
    if (batchSummary.pass === runTotalCount) return "all_pass";
    if (batchSummary.review === runTotalCount) return "all_review";
    return "mixed";
  }, [runTotalCount, batchSummary]);

  function handleBatchResults(items: BatchResultItem[], totalCount: number) {
    setBatchResults(items);
    setRunTotalCount(totalCount);
    if (totalCount > 1 && items.length === 0) {
      setSelectedFilename(null);
      setSingleResult(null);
    }
  }

  function handleClearResults() {
    setSingleResult(null);
    setBatchResults([]);
    setRunTotalCount(0);
    setSelectedFilename(null);
  }

  useEffect(() => {
    if (runTotalCount <= 1) {
      setSelectedFilename(batchResults[0]?.filename ?? null);
      return;
    }
    if (!selectedFilename) {
      setSingleResult(null);
      return;
    }
    const selectedItem = batchResults.find((item) => item.filename === selectedFilename);
    setSingleResult(selectedItem?.result ?? null);
  }, [batchResults, runTotalCount, selectedFilename]);

  async function runPdfTableDebug() {
    if (!debugPdf) {
      setDebugError("Choose a PDF first.");
      return;
    }
    setDebugLoading(true);
    setDebugError(null);
    try {
      const result = await debugPdfTable(debugPdf);
      setDebugResult(result);
      if (result.error) {
        setDebugError(result.error);
      }
    } catch (error) {
      setDebugError(error instanceof Error ? error.message : "Table debug request failed.");
      setDebugResult(null);
    } finally {
      setDebugLoading(false);
    }
  }

  async function loadPdfPicker() {
    if (!pickerPdf) {
      setPickerError("Choose a PDF first.");
      return;
    }
    setPickerLoading(true);
    setPickerError(null);
    setPickerResult(null);
    setPickerPoints([]);
    setPickerCoords(null);
    try {
      const result = await debugPdfPicker(pickerPdf, 0);
      setPickerResult(result);
      if (result.error) {
        setPickerError(result.error);
      }
    } catch (error) {
      setPickerError(error instanceof Error ? error.message : "Failed to load PDF.");
      setPickerResult(null);
    } finally {
      setPickerLoading(false);
    }
  }

  function pixelToPdf(
    pixelX: number,
    pixelY: number,
    r: PdfPickerResponse
  ): { x: number; y: number } | null {
    const pw = r.page_width ?? 0;
    const ph = r.page_height ?? 0;
    const iw = r.image_width ?? 1;
    const ih = r.image_height ?? 1;
    if (pw <= 0 || ph <= 0 || iw <= 0 || ih <= 0) return null;
    return {
      x: Math.round((pixelX / iw) * pw * 100) / 100,
      y: Math.round((pixelY / ih) * ph * 100) / 100
    };
  }

  function handlePickerImageClick(e: React.MouseEvent<HTMLImageElement>) {
    if (!pickerResult?.image_base64) return;
    const img = e.currentTarget;
    const rect = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / rect.width;
    const scaleY = img.naturalHeight / rect.height;
    const pixelX = (e.clientX - rect.left) * scaleX;
    const pixelY = (e.clientY - rect.top) * scaleY;
    const pdf = pixelToPdf(pixelX, pixelY, pickerResult);
    if (pdf) {
      setPickerPoints((prev) => [...prev, pdf]);
    }
  }

  function handlePickerImageMouseMove(e: React.MouseEvent<HTMLImageElement>) {
    if (!pickerResult?.image_base64) return;
    const img = e.currentTarget;
    const rect = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / rect.width;
    const scaleY = img.naturalHeight / rect.height;
    const pixelX = (e.clientX - rect.left) * scaleX;
    const pixelY = (e.clientY - rect.top) * scaleY;
    const pdf = pixelToPdf(pixelX, pixelY, pickerResult);
    setPickerCoords(pdf);
  }

  function handlePickerImageMouseLeave() {
    setPickerCoords(null);
  }

  useEffect(() => {
    const syncPageFromHash = () => setIsDebugPage(window.location.hash === "#debug");
    syncPageFromHash();
    window.addEventListener("hashchange", syncPageFromHash);
    return () => window.removeEventListener("hashchange", syncPageFromHash);
  }, []);

  return (
    <main className="app">
      <header className="app-header">
        <a className="top-link" href={isDebugPage ? "#" : "#debug"} style={{ textAlign: "right", width: "100%" }}>
          {isDebugPage ? "Home" : "Debug"}
        </a>
      </header>

      {isDebugPage ? (
        <>
        <div>
          <h1>Debug</h1>
          <p>PDF tools for table visualization and coordinate picking.</p>
        </div>
          <section className="panel">
            <h2>PDF Table Debug</h2>
            <p>Upload an application PDF to visualize table lines and detected cell boxes from pdfplumber.</p>
            <div className="grid" style={{ gridTemplateColumns: "2fr 1fr", gap: 12, alignItems: "center" }}>
              <input
                type="file"
                accept=".pdf,application/pdf"
                onChange={(event) => {
                  const file = event.target.files?.[0] ?? null;
                  setDebugPdf(file);
                }}
              />
              <button type="button" onClick={runPdfTableDebug} disabled={!debugPdf || debugLoading}>
                {debugLoading ? "Running..." : "Run Table Debug"}
              </button>
            </div>
            {debugError && <p className="vlm-error-banner">{debugError}</p>}
            {debugResult && (
              <>
                <p>
                  Page: {debugResult.page_width ?? "?"} x {debugResult.page_height ?? "?"} | Tables:{" "}
                  {debugResult.table_count ?? 0} | Cells: {debugResult.cell_count ?? 0}
                </p>
                <details>
                  <summary>Detected grid lines</summary>
                  <pre className="raw-text-block">
                    {JSON.stringify(
                      {
                        vertical_lines: debugResult.vertical_lines ?? [],
                        horizontal_lines: debugResult.horizontal_lines ?? []
                      },
                      null,
                      2
                    )}
                  </pre>
                </details>
                <details>
                  <summary>Table preview text</summary>
                  <pre className="raw-text-block">{JSON.stringify(debugResult.table_preview ?? [], null, 2)}</pre>
                </details>
                <h3>Overlay</h3>
                {debugResult.svg_overlay ? (
                  <div style={{ overflow: "auto", border: "1px solid #ddd", background: "#fff", padding: 8 }}>
                    <div dangerouslySetInnerHTML={{ __html: debugResult.svg_overlay }} />
                  </div>
                ) : (
                  <p>No SVG overlay returned.</p>
                )}
              </>
            )}
          </section>

          <section className="panel">
            <h2>PDF Coordinate Picker</h2>
            <p>
              Upload a PDF, then click anywhere on the page to get exact pdfplumber coordinates (x, y in points).
              Use these for explicit_vertical_lines, explicit_horizontal_lines, or cell bboxes.
            </p>
            <div className="grid" style={{ gridTemplateColumns: "2fr 1fr", gap: 12, alignItems: "center" }}>
              <input
                type="file"
                accept=".pdf,application/pdf"
                onChange={(e) => {
                  setPickerPdf(e.target.files?.[0] ?? null);
                }}
              />
              <button type="button" onClick={loadPdfPicker} disabled={!pickerPdf || pickerLoading}>
                {pickerLoading ? "Loading..." : "Load PDF"}
              </button>
            </div>
            {pickerError && <p className="vlm-error-banner">{pickerError}</p>}
            {pickerResult?.image_base64 && (
              <>
                <p>
                  Page: {pickerResult.page_width} × {pickerResult.page_height} pt | Image:{" "}
                  {pickerResult.image_width} × {pickerResult.image_height} px
                  {pickerCoords && (
                    <span style={{ marginLeft: 12 }}>
                      | Cursor: <strong>x={pickerCoords.x.toFixed(2)} y={pickerCoords.y.toFixed(2)}</strong>
                    </span>
                  )}
                </p>
                <div style={{ overflow: "auto", border: "1px solid #ddd", background: "#f8f8f8", padding: 8 }}>
                  <img
                    src={`data:image/png;base64,${pickerResult.image_base64}`}
                    alt="PDF page"
                    style={{ maxWidth: "100%", height: "auto", cursor: "crosshair" }}
                    onClick={handlePickerImageClick}
                    onMouseMove={handlePickerImageMouseMove}
                    onMouseLeave={handlePickerImageMouseLeave}
                  />
                </div>
                <h3>Clicked points (PDF coordinates)</h3>
                {pickerPoints.length === 0 ? (
                  <p>Click on the image above to add points. Use x values for vertical lines, y values for horizontal lines.</p>
                ) : (
                  <>
                    <p>Points (click to add):</p>
                    <pre className="raw-text-block">
                      {pickerPoints
                        .map((p, i) => `#${i + 1}: x=${p.x.toFixed(2)}, y=${p.y.toFixed(2)}`)
                        .join("\n")}
                    </pre>
                    <p>Copy for pdfplumber explicit lines:</p>
                    <pre className="raw-text-block">
                      vertical_lines = [{Array.from(new Set(pickerPoints.map((p) => p.x))).sort((a, b) => a - b).map((x) => x.toFixed(2)).join(", ")}]
                    </pre>
                    <pre className="raw-text-block">
                      horizontal_lines = [{Array.from(new Set(pickerPoints.map((p) => p.y))).sort((a, b) => a - b).map((y) => y.toFixed(2)).join(", ")}]
                    </pre>
                    <button
                      type="button"
                      onClick={() => setPickerPoints([])}
                      style={{ marginTop: 8 }}
                    >
                      Clear points
                    </button>
                  </>
                )}
              </>
            )}
          </section>
        </>
      ) : (
        <>
          <ReviewPanel onResult={setSingleResult} onBatchResult={handleBatchResults} onClearResults={handleClearResults} />

          {runTotalCount > 0 && (
            <section className="panel">
              <h2>{runTotalCount > 1 ? "Batch Results" : "Result"}</h2>
              <p>
                Processed: {batchSummary.completed} / {runTotalCount} | Pass: {batchSummary.pass} | Needs review:{" "}
                {batchSummary.review}
              </p>
              {overallMultiStatus === "all_pass" && <p><strong>All checks passed.</strong> Click on a file to view the results.</p>}
              {overallMultiStatus === "all_review" && <p><strong>All checks need review.</strong> Click on a file to view the results.</p>}
              {overallMultiStatus === "mixed" && <p><strong>Mixed outcomes across files.</strong> Click on a file to view the results.</p>}

              {runTotalCount > 1 && batchResults.length > 0 && (
                <div className="result-list">
                  {batchResults.map((item) => (
                    <button
                      key={item.filename}
                      type="button"
                      className="result-row"
                      onClick={() => {
                        setSelectedFilename(item.filename);
                        setSingleResult(item.result);
                      }}
                    >
                      <span>{item.filename}</span>
                      <StatusBadge status={item.result.status} />
                    </button>
                  ))}
                </div>
              )}
            </section>
          )}

          {singleResult && (runTotalCount <= 1 || selectedFilename) && (
            <section className="panel">
              <h2>
                Verification Result
                {selectedFilename ? ` (${selectedFilename})` : ""}
                <StatusBadge status={singleResult.status} />
              </h2>
              <p>
                Beverage type: <strong>{singleResult.beverage_type}</strong> | Brand score:{" "}
                {singleResult.reconciliation.brand_match_score.toFixed(2)} | Address score:{" "}
                {singleResult.reconciliation.address_match_score.toFixed(2)}
              </p>
              {(() => {
                const grouped = groupFindingsBySection(singleResult.findings);
                const order = ["application_match", "regulatory_label_presence", "label_review", "other"];
                return (
                  <ul className="findings-list">
                    {order.filter((s) => grouped[s]?.length).map((section) => (
                      <li key={section} className="finding-group">
                        <h4 className="finding-group-title">{SECTION_LABELS[section] ?? section}</h4>
                        <ul>
                          {grouped[section].map((finding, idx) => (
                            <FindingItem finding={finding} key={`${finding.code}-${idx}`} />
                          ))}
                        </ul>
                      </li>
                    ))}
                  </ul>
                );
              })()}
              <div className="extracted-grid">
                <section className="evidence-card">
                  <h3>Label Extracted Fields</h3>
                  <ValueRow label="Brand" value={singleResult.label_evidence.brand_name?.value ?? undefined} />
                  <ValueRow label="Class / Type" value={singleResult.label_evidence.class_type?.value ?? undefined} />
                  <ValueRow label="ABV" value={singleResult.label_evidence.abv?.value ?? undefined} />
                  <ValueRow label="Net contents" value={singleResult.label_evidence.net_contents?.value ?? undefined} />
                  <ValueRow label="Bottler / Address" value={singleResult.label_evidence.address?.value ?? undefined} />
                  <ValueRow
                    label="Government warning"
                    value={formatGovernmentWarning(singleResult.label_evidence.government_warning?.value)}
                  />
                </section>
                <section className="evidence-card">
                  <h3>Application Extracted Fields</h3>
                  <ValueRow label="Source of Product" value={singleResult.application_evidence.source_of_product} />
                  <ValueRow label="Brand Name" value={singleResult.application_evidence.brand_name} />
                  <ValueRow label="Fanciful Name" value={singleResult.application_evidence.fanciful_name} />
                  <ValueRow label="Grape Varietals" value={singleResult.application_evidence.grape_varietals} />
                  <ValueRow label="Wine Appellation" value={singleResult.application_evidence.wine_appellation} />
                  <ValueRow label="Bottler Name / Address" value={singleResult.application_evidence.bottler_name_address} />
                </section>
                <section className="evidence-card">
                  <h3>Raw Label Output</h3>
                  <pre className="raw-text-block">{singleResult.label_evidence.raw_text || "No label text returned."}</pre>
                </section>
                <section className="evidence-card">
                  <h3>Raw Application Output</h3>
                  <pre className="raw-text-block">
                    {singleResult.application_evidence.raw_pdfplumber_text?.trim()
                      ? singleResult.application_evidence.raw_pdfplumber_text
                      : "No pdfplumber text returned."}
                  </pre>
                </section>
              </div>
            </section>
          )}

        </>
      )}
    </main>
  );
}

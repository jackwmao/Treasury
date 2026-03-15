import { DragEvent, FormEvent, useRef, useState } from "react";
import { BatchResultItem, VerificationResult, verifySingle } from "../lib/api";

type Props = {
  onResult: (result: VerificationResult | null) => void;
  onBatchResult: (items: BatchResultItem[], totalCount: number) => void;
  onClearResults: () => void;
};

const MAX_CONCURRENT = 3;

export function ReviewPanel({ onResult, onBatchResult, onClearResults }: Props) {
  const [pdfs, setPdfs] = useState<File[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  function fileKey(file: File): string {
    return `${file.name}::${file.size}::${file.lastModified}`;
  }

  function addFiles(files: File[]) {
    const pdfOnly = files.filter((file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"));
    if (!pdfOnly.length) return;
    setPdfs((prev) => {
      const existing = new Set(prev.map(fileKey));
      const additions = pdfOnly.filter((f) => !existing.has(fileKey(f)));
      return [...prev, ...additions];
    });
  }

  function removeFile(target: File) {
    setPdfs((prev) => prev.filter((file) => fileKey(file) !== fileKey(target)));
  }

  function openFilePicker() {
    fileInputRef.current?.click();
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setIsDragging(false);
    addFiles(Array.from(e.dataTransfer.files ?? []));
  }

  function onDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    if (!isDragging) setIsDragging(true);
  }

  function onDragLeave() {
    setIsDragging(false);
  }

  function clearAll() {
    setPdfs([]);
    setError(null);
    onClearResults();
  }

  async function processOne(pdf: File): Promise<BatchResultItem> {
    const result = await verifySingle(pdf);
    return { filename: pdf.name, result };
  }

  async function processMany(files: File[]) {
    const orderedResults: Array<BatchResultItem | null> = new Array(files.length).fill(null);
    let nextIndex = 0;
    let active = 0;
    let done = 0;

    await new Promise<void>((resolve) => {
      const launchNext = () => {
        while (active < MAX_CONCURRENT && nextIndex < files.length) {
          const currentIndex = nextIndex++;
          const file = files[currentIndex];
          active += 1;

          processOne(file)
            .then((item) => {
              orderedResults[currentIndex] = item;
            })
            .catch((err) => {
              setError(`${file.name}: ${err instanceof Error ? err.message : "Request failed"}`);
            })
            .finally(() => {
              active -= 1;
              done += 1;
              const completed = orderedResults.filter((item): item is BatchResultItem => item !== null);
              onBatchResult(completed, files.length);

              if (done >= files.length) {
                resolve();
              } else {
                launchNext();
              }
            });
        }
      };
      launchNext();
    });
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (pdfs.length === 0) {
      setError("Upload at least one application PDF.");
      return;
    }
    setError(null);
    onResult(null);
    onBatchResult([], pdfs.length);
    setLoading(true);
    try {
      if (pdfs.length === 1) {
        const item = await processOne(pdfs[0]);
        onBatchResult([item], 1);
        onResult(item.result);
      } else {
        await processMany(pdfs);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }

  const errorLines = error
    ? error
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
    : [];
  const isRunning = loading;

  return (
    <section className="panel" style={{ backgroundColor: "transparent", border: "none", marginTop: "10%" }}>
      <h2 style={{ textAlign: "center" }}>AI-Powered TTB Label Verification</h2>
      <p style={{ textAlign: "center", marginBottom: "50px" }}>Upload application PDFs with affixed labels to verify compliance with TTB regulations.</p>
      <form onSubmit={onSubmit} className="form">
        <input
          ref={fileInputRef}
          type="file"
          accept="application/pdf"
          multiple
          style={{ display: "none" }}
          onChange={(e) => {
            addFiles(Array.from(e.target.files ?? []));
            e.currentTarget.value = "";
          }}
        />
        <div
          className={`dropzone ${isDragging ? "is-dragging" : ""}`}
          onDrop={onDrop}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          role="button"
          tabIndex={0}
          onClick={openFilePicker}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openFilePicker();
            }
          }}
          aria-label="Drag and drop PDFs or click to browse"
        >
          {pdfs.length === 0 && (
            <div className="dropzone-hint">
              <p className="dropzone-title">Drag and drop PDF files here</p>
              <p className="subtle">or click to browse</p>
            </div>
          )}
          {pdfs.length > 0 && (
            <div className="file-chip-grid">
              {pdfs.map((file) => (
                <div key={fileKey(file)} className="file-chip">
                  <button
                    type="button"
                    className="file-chip-remove"
                    onClick={(e) => {
                      e.stopPropagation();
                      removeFile(file);
                    }}
                    disabled={loading}
                    aria-label={`Remove ${file.name}`}
                  >
                    x
                  </button>
                  <span className="file-chip-name">{file.name}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <p className="subtle">
          {pdfs.length} file{pdfs.length === 1 ? "" : "s"} added
        </p>
        <div className="form-actions-centered">
          <button type="button" className="btn-danger" onClick={clearAll} disabled={isRunning || pdfs.length === 0}>
            Clear
          </button>
          <button className="btn-success" disabled={isRunning || pdfs.length === 0} type="submit">
            {isRunning ? "Running..." : "Run Checks"}
          </button>
        </div>
      </form>
      {error && (
        <p className="error">
          {errorLines.map((line, idx) => (
            <span key={`${line}-${idx}`}>
              {line}
              {idx < errorLines.length - 1 ? <br /> : null}
            </span>
          ))}
        </p>
      )}
    </section>
  );
}

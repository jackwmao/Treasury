export type ReviewStatus = "pass" | "fail" | "needs_review";

export interface ComplianceFinding {
  cfr_part: string;
  cfr_section: string;
  code: string;
  status: ReviewStatus;
  message: string;
  confidence: number;
}

export interface FieldExtraction {
  value: string | null;
  confidence: number;
}

export interface LabelEvidence {
  brand_name: FieldExtraction;
  class_type: FieldExtraction;
  abv: FieldExtraction;
  net_contents: FieldExtraction;
  address: FieldExtraction;
  government_warning: FieldExtraction;
  raw_text: string;
  confidence_score: number;
}

export interface ApplicationEvidence {
  domestic?: boolean | null;
  imported?: boolean | null;
  wine?: boolean | null;
  distilled_spirits?: boolean | null;
  malt_beverages?: boolean | null;
  source_of_product?: string | null;
  brand_name: string | null;
  fanciful_name?: string | null;
  grape_varietals?: string | null;
  wine_appellation?: string | null;
  bottler_name_address: string | null;
  beverage_type: string;
  raw_text: string;
  raw_pdfplumber_text?: string;
}

export interface PdfTableDebugResponse {
  page_width?: number;
  page_height?: number;
  vertical_lines?: number[];
  horizontal_lines?: number[];
  cell_count?: number;
  table_count?: number;
  table_preview?: Array<Array<Array<string | null>>>;
  svg_overlay?: string;
  error?: string;
}

export interface VerificationResult {
  status: ReviewStatus;
  beverage_type: string;
  findings: ComplianceFinding[];
  reconciliation: {
    brand_match_score: number;
    address_match_score: number;
    field_match_scores: Record<string, number>;
  };
  label_evidence: LabelEvidence;
  application_evidence: ApplicationEvidence;
}

export interface BatchResultItem {
  filename: string;
  result: VerificationResult;
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

async function buildApiError(response: Response): Promise<Error> {
  const raw = await response.text();
  try {
    const parsed = JSON.parse(raw) as {
      result?: string;
      message?: string;
      detail?: string | { result?: string; message?: string };
    };
    if (parsed.result === "rejected" && parsed.message) {
      return new Error(parsed.message);
    }
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return new Error(parsed.detail);
    }
    if (
      parsed.detail &&
      typeof parsed.detail === "object" &&
      parsed.detail.result === "rejected" &&
      parsed.detail.message
    ) {
      return new Error(parsed.detail.message);
    }
  } catch {
    // Fall through to raw text payload.
  }
  return new Error(raw || `Request failed with status ${response.status}`);
}

export async function verifySingle(pdf: File): Promise<VerificationResult> {
  const form = new FormData();
  form.append("application_pdf", pdf);
  const response = await fetch(`${API_BASE}/api/verify`, {
    method: "POST",
    body: form
  });
  if (!response.ok) {
    throw await buildApiError(response);
  }
  return response.json();
}

export async function debugPdfTable(pdf: File): Promise<PdfTableDebugResponse> {
  const form = new FormData();
  form.append("application_pdf", pdf);

  const response = await fetch(`${API_BASE}/api/debug/pdf-table`, {
    method: "POST",
    body: form
  });
  if (!response.ok) {
    throw await buildApiError(response);
  }
  return response.json();
}

export interface PdfPickerResponse {
  page_width?: number;
  page_height?: number;
  image_width?: number;
  image_height?: number;
  image_base64?: string;
  error?: string;
}

export async function debugPdfPicker(pdf: File, page = 0): Promise<PdfPickerResponse> {
  const form = new FormData();
  form.append("application_pdf", pdf);
  form.append("page", String(page));

  const response = await fetch(`${API_BASE}/api/debug/pdf-picker`, {
    method: "POST",
    body: form
  });
  if (!response.ok) {
    throw await buildApiError(response);
  }
  return response.json();
}


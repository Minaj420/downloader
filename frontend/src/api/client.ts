export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface ApiErrorBody {
  code?: string;
  message?: string;
  retryable?: boolean;
  analysis_id?: string | null;
}

export class ApiRequestError extends ApiError {
  readonly code: string;
  readonly retryable: boolean;
  readonly analysisId: string | null;

  constructor(status: number, body: ApiErrorBody | undefined, fallback: string) {
    super(status, body?.message ?? fallback);
    this.name = "ApiRequestError";
    this.code = body?.code ?? "unknown_error";
    this.retryable = body?.retryable ?? false;
    this.analysisId = body?.analysis_id ?? null;
  }
}

async function toApiError(path: string, response: Response): Promise<ApiRequestError> {
  let body: ApiErrorBody | undefined;
  try {
    const parsed: unknown = await response.json();
    if (parsed && typeof parsed === "object" && "error" in parsed) {
      body = (parsed as { error?: ApiErrorBody }).error;
    }
  } catch {
    body = undefined;
  }
  return new ApiRequestError(response.status, body, `Request failed: ${path} (${response.status})`);
}

export async function apiGet<TResponse>(path: string): Promise<TResponse> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw await toApiError(path, response);
  }
  return (await response.json()) as TResponse;
}

/**
 * DELETE with no request body. Successful responses carry no content
 * (204 No Content), so nothing is parsed — the promise simply resolves.
 * Errors go through the shared ApiError mapping.
 */
export async function apiDelete(path: string): Promise<void> {
  const response = await fetch(path, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw await toApiError(path, response);
  }
}

export async function apiPost<TResponse, TRequest>(path: string, body: TRequest): Promise<TResponse> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw await toApiError(path, response);
  }
  return (await response.json()) as TResponse;
}

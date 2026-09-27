export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiGet<TResponse>(path: string): Promise<TResponse> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new ApiError(response.status, `Request failed: ${path} (${response.status})`);
  }
  return (await response.json()) as TResponse;
}

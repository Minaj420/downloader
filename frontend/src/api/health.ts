import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./client";

export interface HealthResponse {
  status: string;
  database: string;
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<HealthResponse>("/api/v1/health"),
    retry: false,
    refetchInterval: 30_000,
  });
}

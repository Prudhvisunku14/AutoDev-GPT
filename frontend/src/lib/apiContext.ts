import { createContext, useContext } from "react";
import { createApiClient, type ApiClient } from "./api";
import { getToken } from "./token";

export const ApiContext = createContext<ApiClient | null>(null);

/** The real client: same-origin /api/v1, proxied by the dev server to the FastAPI backend. */
export function defaultApiClient(): ApiClient {
  return createApiClient({ baseUrl: import.meta.env.VITE_API_BASE_URL || "/api/v1", getToken });
}

export function useApi(): ApiClient {
  const client = useContext(ApiContext);
  if (!client) throw new Error("useApi needs an ApiProvider");
  return client;
}

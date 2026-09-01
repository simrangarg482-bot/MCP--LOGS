// `VITE_API_BASE_URL` is compiled in at build time (see frontend/Dockerfile).
// Treat an empty string the same as unset: a Dockerfile `ARG` that is never
// passed still expands to `""`, which `??` alone would not fall back from,
// leaving every API call pointed at a relative path.
const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
export const API_BASE_URL: string =
  configuredApiBaseUrl && configuredApiBaseUrl.length > 0
    ? configuredApiBaseUrl
    : "http://localhost:8000";

/**
 * Central switch between the mock data layer (src/mocks) and the real
 * FastAPI backend. Flip VITE_USE_MOCK_DATA=false once the endpoints
 * referenced in src/api/* are reachable — no call-site changes required.
 */
export const USE_MOCK_DATA: boolean = import.meta.env.VITE_USE_MOCK_DATA !== "false";

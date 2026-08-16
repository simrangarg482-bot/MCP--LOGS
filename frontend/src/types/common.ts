export type UUID = string;
export type ISODateString = string;

export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

// Matches the real error body every `EKIPError` subclass produces
// (`app.api.errors.ekip_error_handler`, `EKIPError.to_error_body()`):
// `{"error_code", "message", "detail"}`. `detail` is frequently a dict
// (e.g. `{"incident_id": "..."}`), never guaranteed to be a string --
// typing it `string` here was inaccurate (callers already had to
// defensively `typeof === "string"` check it before use).
export interface ApiError {
  status: number;
  message: string;
  errorCode?: string;
  detail?: unknown;
}

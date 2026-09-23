import { HttpErrorResponse } from '@angular/common/http';

/** A failed API call, carrying a message that is safe to show to the user. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    /** Seconds to wait before retrying, when the API said so (HTTP 429). */
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  get isRateLimited(): boolean {
    return this.status === 429;
  }
}

const UNREACHABLE = 'Could not reach the server. Make sure the backend is running and try again.';
const INVALID_INPUT = 'Please check the name and email address and try again.';
const SERVER_FAILURE = 'Something went wrong on the server. Please try again later.';
const UNEXPECTED = 'An unexpected error occurred. Please try again.';
const RATE_LIMITED = 'Too many confirmation requests. Please wait a moment and try again.';

/**
 * Translate any HTTP failure into an ApiError with a user-facing message.
 * The backend returns `{ "detail": "<readable message>" }` for expected failures such as
 * a rejected email send; anything else gets a generic message so internals never leak.
 */
export function toApiError(error: unknown): ApiError {
  if (!(error instanceof HttpErrorResponse)) {
    return new ApiError(UNEXPECTED, -1);
  }
  if (error.status === 0) {
    return new ApiError(UNREACHABLE, 0);
  }
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  if (error.status === 429) {
    // The backend phrases the wait in its detail message; the header is kept for callers.
    const message = typeof detail === 'string' ? detail : RATE_LIMITED;
    return new ApiError(message, error.status, parseRetryAfter(error.headers.get('Retry-After')));
  }
  if (typeof detail === 'string') {
    return new ApiError(detail, error.status);
  }
  if (error.status === 422) {
    return new ApiError(INVALID_INPUT, error.status);
  }
  return new ApiError(SERVER_FAILURE, error.status);
}

function parseRetryAfter(header: string | null): number | null {
  const seconds = Number(header);
  return header !== null && Number.isInteger(seconds) && seconds > 0 ? seconds : null;
}

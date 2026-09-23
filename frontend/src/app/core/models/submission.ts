// Wire types for the /api/v1/submissions endpoints. Field names mirror the JSON the API returns.

export interface SubmissionRequest {
  name: string;
  email: string;
}

/**
 * The API returns a message and an opaque tracking id on success: no echo of the input and
 * no record details. What was sent is already known from the form.
 */
export interface SubmissionResponse {
  detail: string;
  /** Opaque handle for the delivery-status endpoint, which returns only the status. */
  tracking_id: string;
}

/** Delivery states in lifecycle order, as stored by the backend. */
export const DELIVERY_STATUSES = [
  'pending',
  'accepted',
  'queued',
  'retrying',
  'delivered',
  'soft_bounced',
  'hard_bounced',
  'blocked',
  'failed',
] as const;

export type DeliveryStatus = (typeof DELIVERY_STATUSES)[number];

/** The whole body of GET /api/v1/submissions/{tracking_id}/status. */
export interface DeliveryStatusResponse {
  status: DeliveryStatus;
}

export function isDeliveryStatus(value: unknown): value is DeliveryStatus {
  return typeof value === 'string' && (DELIVERY_STATUSES as readonly string[]).includes(value);
}

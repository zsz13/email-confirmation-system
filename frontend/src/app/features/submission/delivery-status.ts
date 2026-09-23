import { DeliveryStatus } from '../../core/models/submission';

/** How a delivery state is shown: the badge text, what it means, and its colour. */
export interface DeliveryStatusView {
  label: string;
  description: string;
  tone: 'progress' | 'success' | 'warning' | 'danger';
}

// Nothing here claims Inbox placement: "delivered" means the recipient's mail server took the
// message, and where it files it (Inbox, Spam, Junk) is out of the sender's hands.
export const DELIVERY_STATUS_VIEWS: Readonly<Record<DeliveryStatus, DeliveryStatusView>> = {
  pending: {
    label: 'Preparing your email',
    description: 'Your email is being handed to the email provider.',
    tone: 'progress',
  },
  accepted: {
    label: 'Accepted for delivery',
    description: 'The email provider accepted the message and will send it shortly.',
    tone: 'progress',
  },
  queued: {
    label: 'Queued by email provider',
    description: 'The message is waiting in the email provider’s sending queue.',
    tone: 'progress',
  },
  retrying: {
    label: 'Delivery is being retried',
    description:
      'The recipient’s mail server has not accepted the message yet. The email provider is ' +
      'retrying automatically.',
    tone: 'warning',
  },
  delivered: {
    label: 'Delivered to recipient mail server',
    description:
      'The recipient’s mail server accepted the message. Check your Inbox, Spam, or Junk folder.',
    tone: 'success',
  },
  soft_bounced: {
    label: 'Temporarily rejected',
    description:
      'The recipient’s mail server temporarily refused the message and the email provider has ' +
      'stopped retrying. Check the address, or try again later.',
    tone: 'warning',
  },
  hard_bounced: {
    label: 'Delivery failed',
    description:
      'The recipient’s mail server permanently rejected the message. Check that the address ' +
      'is correct.',
    tone: 'danger',
  },
  blocked: {
    label: 'Blocked by email provider',
    description:
      'The email provider did not send this message. Try again later or use a different address.',
    tone: 'danger',
  },
  failed: {
    label: 'Could not send email',
    description: 'The message could not be handed to the email provider. Please try again later.',
    tone: 'danger',
  },
};

/** States the backend will not move on from, so there is nothing left to poll for. */
export const TERMINAL_DELIVERY_STATUSES: ReadonlySet<DeliveryStatus> = new Set([
  'delivered',
  'soft_bounced',
  'hard_bounced',
  'blocked',
  'failed',
]);

/** How often the page reads the stored status while delivery is in progress. */
export const STATUS_POLL_INTERVAL_MS = 5_000;
/** How long the page keeps polling on its own; the backend keeps reconciling after that. */
export const STATUS_POLL_WINDOW_MS = 120_000;

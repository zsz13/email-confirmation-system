import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  inject,
  output,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Subscription } from 'rxjs';

import { DeliveryStatus } from '../../core/models/submission';
import { ApiError } from '../../core/services/api-error';
import { SubmissionApi } from '../../core/services/submission-api';
import {
  DELIVERY_STATUS_VIEWS,
  STATUS_POLL_INTERVAL_MS,
  STATUS_POLL_WINDOW_MS,
  TERMINAL_DELIVERY_STATUSES,
} from './delivery-status';

const NAME_MAX_LENGTH = 100;
const EMAIL_MAX_LENGTH = 254;

// Angular's built-in email validator accepts "user@host"; the backend requires a dotted domain.
const EMAIL_WITH_DOMAIN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const NOT_BLANK = /\S/;

// A 201 means the provider accepted the email; whether and where it lands is not known yet,
// so nothing here claims delivery.
export const SPAM_NOTICE =
  'If you don’t see it shortly, check your Spam or Junk folder. This demo uses a sender on a ' +
  'public email domain, so some providers may flag the message.';

type FormState =
  | { status: 'idle' }
  | { status: 'submitting' }
  // The address comes from the form: the API does not echo it back. The tracking id is the
  // opaque handle the API gave out for reading this submission's delivery status.
  | { status: 'success'; email: string; trackingId: string }
  | { status: 'error'; title: string; message: string };

@Component({
  selector: 'app-submission-form',
  imports: [DatePipe, ReactiveFormsModule],
  templateUrl: './submission-form.html',
  styleUrl: './submission-form.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SubmissionForm {
  private readonly api = inject(SubmissionApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly formBuilder = inject(NonNullableFormBuilder);

  protected readonly spamNotice = SPAM_NOTICE;
  protected readonly pollIntervalSeconds = STATUS_POLL_INTERVAL_MS / 1000;

  protected readonly form = this.formBuilder.group({
    name: [
      '',
      [Validators.required, Validators.pattern(NOT_BLANK), Validators.maxLength(NAME_MAX_LENGTH)],
    ],
    email: [
      '',
      [
        Validators.required,
        Validators.email,
        Validators.pattern(EMAIL_WITH_DOMAIN),
        Validators.maxLength(EMAIL_MAX_LENGTH),
      ],
    ],
  });

  /** True once the provider accepted a confirmation, false again when the person starts over. */
  readonly sent = output<boolean>();

  protected readonly state = signal<FormState>({ status: 'idle' });
  protected readonly submitting = computed(() => this.state().status === 'submitting');
  // One persistent live region: screen readers announce updates to it, while a freshly
  // inserted role="status" element is not announced everywhere.
  protected readonly announcement = computed(() => {
    const current = this.state();
    if (current.status === 'submitting') {
      return 'Sending your confirmation email.';
    }
    if (current.status === 'success') {
      return (
        `Confirmation email to ${current.email} accepted for delivery. ` +
        `Delivery status: ${this.deliveryView().label}.`
      );
    }
    return '';
  });
  private readonly submitAttempted = signal(false);

  // Delivery tracking. The page only reads the status the backend has stored; the backend's
  // reconciler is what asks the email provider, whether or not this page is still polling.
  protected readonly delivery = signal<DeliveryStatus>('accepted');
  protected readonly deliveryView = computed(() => DELIVERY_STATUS_VIEWS[this.delivery()]);
  protected readonly deliveryFinal = computed(() =>
    TERMINAL_DELIVERY_STATUSES.has(this.delivery()),
  );
  /** True while the page checks on its own; false once final or the polling window closed. */
  protected readonly autoPolling = signal(false);
  /** True while a status request is in flight; at most one ever is. */
  protected readonly checking = signal(false);
  protected readonly statusError = signal<string | null>(null);
  protected readonly lastChecked = signal<Date | null>(null);
  private pollTimer: ReturnType<typeof setTimeout> | undefined;
  private pollDeadline = 0;
  private statusRequest: Subscription | undefined;

  constructor() {
    this.destroyRef.onDestroy(() => this.stopTracking());
  }

  protected submit(): void {
    // A second click while a request is in flight must not create a duplicate submission.
    if (this.submitting()) {
      return;
    }
    this.submitAttempted.set(true);
    if (this.form.invalid) {
      this.focusFirstInvalidField();
      return;
    }

    const request = {
      name: this.form.controls.name.value.trim(),
      email: this.form.controls.email.value.trim(),
    };
    this.state.set({ status: 'submitting' });
    this.form.disable();

    this.api
      .createSubmission(request)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (response) => {
          this.state.set({
            status: 'success',
            email: request.email,
            trackingId: response.tracking_id,
          });
          this.sent.emit(true);
          this.startTracking();
        },
        error: (error: unknown) => {
          this.form.enable();
          this.state.set(describeFailure(error));
        },
      });
  }

  /**
   * Read the stored delivery status once. Used by the automatic polling and by the "Check
   * delivery status" button alike, so the two can never have requests in flight together.
   */
  protected checkStatus(): void {
    const current = this.state();
    if (current.status !== 'success' || this.checking()) {
      return;
    }
    // A manual check replaces the pending automatic one; polling resumes after it answers.
    this.clearPollTimer();
    this.checking.set(true);
    this.statusRequest = this.api.getDeliveryStatus(current.trackingId).subscribe({
      next: (status) => {
        this.delivery.set(status);
        this.statusError.set(null);
        this.lastChecked.set(new Date());
        this.settleCheck();
      },
      error: (error: unknown) => {
        // The last known status stays on screen; only the failed check is reported.
        this.statusError.set(
          error instanceof ApiError ? error.message : 'An unexpected error occurred.',
        );
        this.settleCheck();
      },
    });
  }

  protected startOver(): void {
    this.stopTracking();
    this.form.reset();
    this.form.enable();
    this.submitAttempted.set(false);
    this.state.set({ status: 'idle' });
    this.sent.emit(false);
    // The form is rendered on the next change detection; focus it once it exists.
    queueMicrotask(() => this.host.nativeElement.querySelector<HTMLInputElement>('#name')?.focus());
  }

  private startTracking(): void {
    this.delivery.set('accepted'); // what a 201 means; the first read confirms or moves it on
    this.statusError.set(null);
    this.lastChecked.set(null);
    this.pollDeadline = Date.now() + STATUS_POLL_WINDOW_MS;
    this.autoPolling.set(true);
    this.scheduleNextPoll();
  }

  private settleCheck(): void {
    this.checking.set(false);
    this.statusRequest = undefined;
    this.scheduleNextPoll();
  }

  /** Queue the next automatic read, or end automatic polling when there is no point. */
  private scheduleNextPoll(): void {
    this.clearPollTimer();
    if (!this.autoPolling()) {
      return;
    }
    if (this.deliveryFinal() || Date.now() + STATUS_POLL_INTERVAL_MS > this.pollDeadline) {
      this.autoPolling.set(false);
      return;
    }
    // A timeout per read rather than an interval: the next read is only queued once the
    // previous one has answered, so a slow response never overlaps the next request.
    this.pollTimer = setTimeout(() => this.checkStatus(), STATUS_POLL_INTERVAL_MS);
  }

  private clearPollTimer(): void {
    clearTimeout(this.pollTimer);
    this.pollTimer = undefined;
  }

  private stopTracking(): void {
    this.clearPollTimer();
    this.autoPolling.set(false);
    this.statusRequest?.unsubscribe();
    this.statusRequest = undefined;
    this.checking.set(false);
  }

  // Errors appear once the person has typed in the field or tried to submit, never for a
  // field that was only tabbed through.
  protected showError(field: 'name' | 'email'): boolean {
    const control = this.form.controls[field];
    return control.invalid && (control.dirty || this.submitAttempted());
  }

  private focusFirstInvalidField(): void {
    const firstInvalid = this.form.controls.name.invalid ? 'name' : 'email';
    this.host.nativeElement.querySelector<HTMLInputElement>(`#${firstInvalid}`)?.focus();
  }

  protected nameError(): string {
    const errors = this.form.controls.name.errors ?? {};
    if ('maxlength' in errors) {
      return `Name must be ${NAME_MAX_LENGTH} characters or fewer.`;
    }
    return 'Enter your name.';
  }

  protected emailError(): string {
    const errors = this.form.controls.email.errors ?? {};
    if ('required' in errors) {
      return 'Enter your email address.';
    }
    if ('maxlength' in errors) {
      return `Email address must be ${EMAIL_MAX_LENGTH} characters or fewer.`;
    }
    return 'Enter a valid email address, like name@example.com.';
  }
}

function describeFailure(error: unknown): FormState {
  if (error instanceof ApiError && error.isRateLimited) {
    return { status: 'error', title: 'Too many requests', message: error.message };
  }
  return {
    status: 'error',
    title: 'The confirmation could not be sent',
    message:
      error instanceof ApiError ? error.message : 'An unexpected error occurred. Please try again.',
  };
}

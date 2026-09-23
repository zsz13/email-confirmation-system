import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/** The application mark: an envelope with a delivered check. public/favicon.svg is its tile variant. */
@Component({
  selector: 'app-logo',
  template: `
    <svg
      [attr.width]="size()"
      [attr.height]="size()"
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="2" y="6" width="24" height="18" rx="4" fill="var(--accent)" />
      <path
        d="M5 10.2 14 16.6 23 10.2"
        stroke="#fff"
        stroke-width="2.2"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <circle cx="24.5" cy="22.5" r="6.5" fill="var(--success)" stroke="#fff" stroke-width="2" />
      <path
        d="M21.6 22.6l2 2 3.6-4"
        stroke="#fff"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
  `,
  styles: `
    :host {
      display: inline-flex;
      flex: none;
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class Logo {
  readonly size = input(32);
}

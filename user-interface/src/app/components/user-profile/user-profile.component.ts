import { Component, DestroyRef, ElementRef, HostListener, OnInit, ViewChild, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { RouterLink } from '@angular/router';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { InitialsAvatarComponent } from '../../shared/avatar/initials-avatar.component';
import { EmptyStateComponent } from '../../shared/empty-state/empty-state.component';
import {
  AVATAR_COLOR_OPTIONS,
  DEFAULT_AVATAR_COLOR,
  resolveAvatarColor,
} from '../../shared/avatar/avatar-colors';
import { UserProfileApiService } from '../../services/user-profile-api.service';
import { UserProfileStore } from '../../services/user-profile-store.service';
import { HasUnsavedChanges } from '../../core/unsaved-changes.guard';
import { NotificationService } from '../../core/notification.service';
import type { Association, ProfileIntegration } from '../../models/user-profile.model';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';

/** A display group of associations sharing one artifact type. */
interface AssociationGroup {
  type: string;
  label: string;
  icon: string;
  /** Route the group's items link to, when the owning team has an editor screen. */
  route?: string;
  /** Query params for the route link (e.g. deep-linking a dashboard tab). */
  queryParams?: Record<string, string>;
  items: Association[];
}

/** Supported artifact types in display order, each with its label and Material icon. */
const ARTIFACT_GROUPS: Omit<AssociationGroup, 'items'>[] = [
  { type: 'brand', label: 'Brands', icon: 'palette' },
  { type: 'blog_post', label: 'Blog Posts', icon: 'article' },
  { type: 'project', label: 'Projects', icon: 'terminal' },
  { type: 'agentic_team', label: 'Agentic Teams', icon: 'groups' },
  // Deep-link straight to the career profile editor tab.
  { type: 'career', label: 'Career', icon: 'work', route: '/job-matching', queryParams: { tab: 'profile' } },
];

/**
 * User Profile page: review/update the single profile (including the initials
 * avatar color) and view the artifacts (brands, blog posts, projects, agentic
 * teams) and integrations linked to it. Reached via the profile icon in the
 * sidenav footer or the "User Profile" entry in the Settings nav group.
 */
@Component({
  selector: 'app-user-profile',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    RouterLink,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    DashboardShellComponent,
    InitialsAvatarComponent,
    EmptyStateComponent,
    InlineBannerComponent,
  ],
  templateUrl: './user-profile.component.html',
  styleUrl: './user-profile.component.scss',
})
export class UserProfileComponent implements OnInit, HasUnsavedChanges {
  private readonly api = inject(UserProfileApiService);
  private readonly fb = inject(FormBuilder);
  private readonly destroyRef = inject(DestroyRef);
  private readonly notifications = inject(NotificationService);
  private readonly profileStore = inject(UserProfileStore);

  loading = false;
  saving = false;
  error: string | null = null;
  /**
   * True once at least one load has succeeded. Saving is blocked until then:
   * submitting the constructor-default form after a failed load would blank
   * the real display name/email/bio on the server (present scalar fields are
   * written verbatim) and reset the avatar color.
   */
  profileLoaded = false;

  /**
   * Set when the user clicks the banner's Retry: after the retried load
   * succeeds, focus moves to the first form field so keyboard/AT users don't
   * lose their place when the (focused) Retry button unmounts.
   */
  private focusFormAfterLoad = false;

  /** First form field, the focus target after a successful banner Retry. */
  @ViewChild('displayNameInput') private displayNameInput?: ElementRef<HTMLInputElement>;

  groups: AssociationGroup[] = [];
  integrations: ProfileIntegration[] = [];
  /** Total linked artifacts, computed once per load (not per change-detection tick). */
  totalAssociations = 0;

  /** Palette rendered as the avatar color swatch radiogroup. */
  readonly avatarColors = AVATAR_COLOR_OPTIONS;

  readonly form = this.fb.group({
    display_name: [''],
    email: ['', [Validators.email]],
    bio: [''],
    avatar_color: [DEFAULT_AVATAR_COLOR],
  });

  ngOnInit(): void {
    this.load();
  }

  /**
   * Whether leaving the page would discard edits (drives the CanDeactivate
   * guard and the beforeunload prompt).
   *
   * Preconditions: none.
   * Postconditions: true iff the form has unsaved edits. This deliberately
   * still reports true DURING an in-flight save: the save request is cancelled
   * if the component is destroyed (`takeUntilDestroyed`), so navigating away
   * mid-save would silently lose the write — the user must be prompted until
   * the save actually completes (which clears `dirty` via `markAsPristine`).
   */
  hasUnsavedChanges(): boolean {
    return this.form.dirty;
  }

  /**
   * Native browser prompt when the tab/window closes with unsaved edits.
   *
   * Preconditions: invoked by the browser's `beforeunload` event.
   * Postconditions: when `hasUnsavedChanges()` is true, cancels the event so
   * the browser shows its generic "leave site?" prompt; otherwise leaves the
   * event untouched (unload proceeds without a prompt).
   */
  @HostListener('window:beforeunload', ['$event'])
  onBeforeUnload(event: BeforeUnloadEvent): void {
    if (this.hasUnsavedChanges()) {
      event.preventDefault(); // the modern trigger for the generic unload prompt
      // Legacy engines (older Chromium/Firefox) only prompt when returnValue is
      // set; assigning a non-empty string keeps the guard working there too.
      event.returnValue = '';
    }
  }

  /**
   * Load the profile, its associations, and integration status in one request.
   *
   * Preconditions: none — a no-op while a previous load is still in flight, so
   * overlapping requests can't race to set the view.
   * Postconditions: on success `groups`/`totalAssociations`/`integrations`
   * reflect the response, and `form` is patched ONLY while pristine — a dirty
   * form keeps the user's unsaved edits (e.g. a "Refresh linked work" click
   * mid-edit must not silently discard them). On a 2xx response whose shape is
   * malformed (missing `profile`/`associations`/`integrations`), `error` is set
   * and `groups`/`integrations`/`totalAssociations` are cleared so a broken
   * contract can't leave stale artifacts on screen. On an HTTP error, `error` is
   * set and any previously loaded data is left as-is (a transient blip keeps the
   * last-known-good view). `loading` is false either way.
   */
  load(): void {
    if (this.loading) return; // guard against overlapping loads (e.g. a refresh re-trigger)
    this.loading = true;
    this.error = null;
    this.api
      .getOverview()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (overview) => {
        // A 2xx response with a malformed body slips past the error handler, so
        // guard the shape before destructuring rather than throw deep in render.
        // `associations`/`integrations` must be arrays — a non-array (e.g. an
        // object) would otherwise throw in `groupAssociations`/the template.
        if (
          !overview?.profile ||
          !Array.isArray(overview.associations) ||
          !Array.isArray(overview.integrations)
        ) {
          // Clear any previously loaded data so stale artifacts/integrations
          // aren't shown alongside the error after a re-load.
          this.groups = [];
          this.integrations = [];
          this.totalAssociations = 0;
          this.error = 'Received an unexpected response from the server.';
          this.loading = false;
          return;
        }
        const { profile, associations, integrations } = overview;
        if (this.form.pristine) {
          this.form.patchValue({
            // `?? ''` defends against a null slipping through a technically-valid
            // body; the backend columns are NOT NULL DEFAULT so this is belt-and-braces.
            display_name: profile.display_name ?? '',
            email: profile.email ?? '',
            bio: profile.bio ?? '',
            // `preferences` is free-form JSONB; the optional chain yields undefined
            // for null/garbage containers and resolveAvatarColor defends the rest.
            avatar_color: resolveAvatarColor(profile.preferences?.['avatar_color']).key,
          });
        }
        // Keep the shared identity (footer avatar) in sync with the server.
        this.profileStore.set(profile.display_name ?? '', profile.preferences?.['avatar_color']);
        this.groups = this.groupAssociations(associations);
        this.totalAssociations = this.groups.reduce((sum, g) => sum + g.items.length, 0);
        this.integrations = integrations;
        this.profileLoaded = true; // the form now reflects real server state — saving is safe
        this.loading = false;
        if (this.focusFormAfterLoad) {
          this.focusFormAfterLoad = false;
          // The form renders on the next change-detection pass, after this
          // handler returns — defer the focus until it exists.
          setTimeout(() => this.displayNameInput?.nativeElement.focus());
        }
      },
      error: () => {
        this.focusFormAfterLoad = false; // the alert announces the failure instead
        this.error = 'Failed to load your profile. Please try again.';
        this.loading = false;
      },
    });
  }

  /**
   * Retry a failed initial load from the error banner.
   *
   * Preconditions: none — delegates to `load()`, which guards re-entry.
   * Postconditions: identical to `load()`, plus on success keyboard focus
   * moves to the first form field (the Retry button unmounts while focused,
   * which would otherwise drop focus to the document body).
   */
  retryLoad(): void {
    this.focusFormAfterLoad = true;
    this.load();
  }

  /**
   * Persist the editable profile fields.
   *
   * Preconditions: none enforced — a no-op until a load has succeeded
   * (`profileLoaded`; see that field for why saving earlier is destructive),
   * a no-op (marking the form touched) when `form.invalid` (e.g. a malformed
   * email), and a no-op while a previous save is still in flight, so a
   * double-submit can't send duplicate updates.
   * Postconditions: when the form is valid and loaded, on success a transient
   * "Profile saved." snackbar is shown and the form is marked pristine; on
   * failure the persistent `error` banner is set. `saving` is false either way.
   */
  save(): void {
    if (!this.profileLoaded) return; // never overwrite the server with unloaded defaults
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    if (this.saving) return; // guard against a double-submit before the button disables
    this.saving = true;
    this.error = null;
    const value = this.form.getRawValue();
    // Send avatar_color only when the user picked a swatch this session: the
    // backend merges preferences key-by-key, so omitting the field leaves the
    // stored value untouched. Re-sending a merely-loaded value would stamp
    // the default onto never-chose profiles and could overwrite a concurrent
    // tab's newer choice with this tab's stale one.
    const avatarColorPicked = this.form.controls.avatar_color.dirty;
    this.api
      .updateProfile({
        display_name: value.display_name ?? '',
        email: value.email ?? '',
        bio: value.bio ?? '',
        ...(avatarColorPicked
          ? { preferences: { avatar_color: value.avatar_color ?? DEFAULT_AVATAR_COLOR } }
          : {}),
      })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.saving = false;
          // Transient confirmation (matches the app's snackbar convention for
          // successful actions); errors stay as persistent banners.
          this.notifications.saved('Profile saved.');
          // Reflect the saved identity in the shared store (footer avatar).
          this.profileStore.set(value.display_name ?? '', value.avatar_color);
          // The form now matches the persisted state — clear the dirty flag so the
          // unsaved-changes guard doesn't prompt after a successful save.
          this.form.markAsPristine();
        },
        error: () => {
          this.saving = false;
          this.error = 'Failed to save your profile. Please try again.';
        },
      });
  }

  /**
   * Select an avatar color swatch.
   *
   * Preconditions: `key` should be a palette key; unknown keys are tolerated
   * (they render — and persist — as the default color via `resolveAvatarColor`).
   * Postconditions: the `avatar_color` control holds `key` and is marked dirty,
   * so unsaved-changes semantics match typing in a field (a bare button click
   * does not dirty a reactive control by itself).
   */
  selectAvatarColor(key: string): void {
    this.form.controls.avatar_color.setValue(key);
    this.form.controls.avatar_color.markAsDirty();
  }

  /**
   * Arrow-key selection within the avatar color radiogroup (WAI-ARIA radio
   * pattern: the group is one tab stop and arrows move the selection).
   *
   * Preconditions: `event.currentTarget` is a `.up-swatch` radio button
   * inside the `.up-swatches` radiogroup (palette order).
   * Postconditions: on Arrow keys the selection moves to the next/previous
   * palette color (wrapping) with the same side effects as a click, focus
   * follows the selection, and the event's default is suppressed; all other
   * keys are left untouched.
   */
  onSwatchKeydown(event: KeyboardEvent): void {
    const delta = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key] ?? 0;
    if (delta === 0) return;
    event.preventDefault();
    // Normalize first so a programmatically-set unknown key still has a
    // well-defined position to move from.
    const currentKey = resolveAvatarColor(this.form.controls.avatar_color.value).key;
    const index = this.avatarColors.findIndex((option) => option.key === currentKey);
    const nextIndex = (index + delta + this.avatarColors.length) % this.avatarColors.length;
    this.selectAvatarColor(this.avatarColors[nextIndex].key);
    const group = (event.currentTarget as HTMLElement).closest('.up-swatches');
    const radios = group?.querySelectorAll<HTMLButtonElement>('.up-swatch');
    radios?.[nextIndex]?.focus();
  }

  /**
   * Group flat associations into the fixed display order. Empty groups are
   * dropped, EXCEPT the Career group: it always renders (as a "set it up"
   * prompt when empty) so a user who hasn't built a career profile can still
   * discover the editor from the profile page.
   */
  private groupAssociations(items: Association[]): AssociationGroup[] {
    return ARTIFACT_GROUPS.map((g) => ({
      ...g,
      items: items.filter((a) => a.artifact_type === g.type),
    })).filter((g) => g.items.length > 0 || g.type === 'career');
  }
}

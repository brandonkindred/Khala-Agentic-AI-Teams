import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatButtonModule } from '@angular/material/button';

/**
 * Tiered + fallback renderer for a small subset of JSON Schema. Emits a
 * ``valueChange`` event with the fully-composed JSON each time a field
 * (or a JSON-fallback subtree) mutates.
 *
 * Handled:
 *   - scalars: string, integer, number, boolean
 *   - known string formats: date, date-time, email, uri
 *   - enum (any type)
 *   - flat arrays of scalar items
 *   - objects nested up to ``MAX_DEPTH`` levels
 *
 * Anything else (unions / anyOf / oneOf > 1, $ref cycles, nested
 * objects beyond MAX_DEPTH, arrays of objects) is rendered as a JSON
 * textarea subtree and marked with an "editing as JSON" chip.
 */
const MAX_DEPTH = 2;

interface SchemaNode {
  type?: string | string[];
  enum?: unknown[];
  format?: string;
  properties?: Record<string, SchemaNode>;
  required?: string[];
  items?: SchemaNode;
  title?: string;
  description?: string;
  default?: unknown;
  anyOf?: SchemaNode[];
  oneOf?: SchemaNode[];
  $ref?: string;
  $defs?: Record<string, SchemaNode>;
  definitions?: Record<string, SchemaNode>;
}

@Component({
  selector: 'app-agent-schema-form',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatSlideToggleModule,
    MatChipsModule,
    MatIconModule,
    MatTooltipModule,
    MatButtonModule,
  ],
  templateUrl: './agent-schema-form.component.html',
  styleUrl: './agent-schema-form.component.scss',
})
export class AgentSchemaFormComponent implements OnChanges {
  @Input() schema: SchemaNode | null = null;
  @Input() value: unknown = {};

  @Output() readonly valueChange = new EventEmitter<unknown>();

  readonly rendered = signal<boolean>(false);
  defs: Record<string, SchemaNode> = {};

  ngOnChanges(changes: SimpleChanges): void {
    if ('schema' in changes && this.schema) {
      this.defs = (this.schema.$defs ?? this.schema.definitions ?? {}) as Record<string, SchemaNode>;
      this.rendered.set(this.canRender(this.schema, 0));
    }
    if ('schema' in changes || 'value' in changes) {
      if (!this.rendered() && this.schema) {
        // Emit a single notification so the parent knows to fall back.
        this.valueChange.emit(this.value);
      }
    }
  }

  // -----------------------------------------------------------------
  // Template helpers
  // -----------------------------------------------------------------

  resolve(node: SchemaNode | null | undefined): SchemaNode | null {
    if (!node) return null;
    if (node.$ref) {
      const key = node.$ref.split('/').pop();
      if (key && this.defs[key]) {
        return { ...this.defs[key], ...node, $ref: undefined };
      }
      return null;
    }
    if (node.anyOf) {
      const nonNull = node.anyOf.find((b) => b.type !== 'null');
      if (nonNull) return this.resolve({ ...node, anyOf: undefined, ...nonNull });
    }
    if (node.oneOf && node.oneOf.length === 1) {
      return this.resolve({ ...node, oneOf: undefined, ...node.oneOf[0] });
    }
    return node;
  }

  canRender(node: SchemaNode | null, depth: number): boolean {
    const resolved = this.resolve(node);
    if (!resolved) return false;
    if (resolved.enum) return true;

    const type = Array.isArray(resolved.type) ? resolved.type.find((t) => t !== 'null') : resolved.type;
    if (!type) {
      // Untyped objects/unions: bail to JSON fallback.
      return false;
    }
    if (type === 'string' || type === 'integer' || type === 'number' || type === 'boolean') {
      return true;
    }
    if (type === 'array') {
      const items = this.resolve(resolved.items);
      if (!items) return false;
      const itemType = Array.isArray(items.type) ? items.type[0] : items.type;
      return itemType === 'string' || itemType === 'integer' || itemType === 'number';
    }
    if (type === 'object') {
      if (depth >= MAX_DEPTH) return false;
      const props = resolved.properties ?? {};
      // Every property must itself be renderable.
      return Object.values(props).every((sub) => this.canRender(sub, depth + 1));
    }
    return false;
  }

  entries(node: SchemaNode | null): [string, SchemaNode][] {
    const resolved = this.resolve(node);
    if (!resolved?.properties) return [];
    return Object.entries(resolved.properties);
  }

  kindOf(node: SchemaNode | null): string {
    const resolved = this.resolve(node);
    if (!resolved) return 'json';
    if (resolved.enum) return 'enum';
    const type = Array.isArray(resolved.type) ? resolved.type.find((t) => t !== 'null') : resolved.type;
    if (type === 'string') {
      const fmt = resolved.format;
      if (fmt === 'date') return 'date';
      if (fmt === 'date-time') return 'datetime';
      if (fmt === 'email') return 'email';
      if (fmt === 'uri' || fmt === 'url') return 'url';
      return 'text';
    }
    if (type === 'integer' || type === 'number') return 'number';
    if (type === 'boolean') return 'boolean';
    if (type === 'array') return 'chips';
    if (type === 'object') return 'object';
    return 'json';
  }

  getField(key: string): unknown {
    if (typeof this.value !== 'object' || this.value === null) return undefined;
    return (this.value as Record<string, unknown>)[key];
  }

  setField(key: string, next: unknown): void {
    const current = typeof this.value === 'object' && this.value !== null ? { ...(this.value as Record<string, unknown>) } : {};
    current[key] = next;
    this.value = current;
    this.valueChange.emit(current);
  }

  // ---- chips-style array handlers ----

  addChip(key: string, token: string, event: Event): void {
    const input = event.target as HTMLInputElement | null;
    const trimmed = token.trim();
    if (!trimmed) return;
    const current = (this.getField(key) as unknown[]) ?? [];
    this.setField(key, [...current, trimmed]);
    if (input) input.value = '';
  }

  removeChip(key: string, index: number): void {
    const current = [...((this.getField(key) as unknown[]) ?? [])];
    current.splice(index, 1);
    this.setField(key, current);
  }

  asArray(value: unknown): unknown[] {
    return Array.isArray(value) ? value : [];
  }

  // ---- nested object handler ----

  onNestedChange(key: string, nested: unknown): void {
    this.setField(key, nested);
  }

  // ---- JSON fallback handler ----

  onJsonFallbackChange(key: string, raw: string): void {
    try {
      const parsed = raw.trim() === '' ? null : JSON.parse(raw);
      this.setField(key, parsed);
    } catch {
      // Do not emit while the JSON is malformed; the parent only cares
      // about successful parses. The user sees the raw text next time.
    }
  }

  jsonFallbackOf(value: unknown): string {
    return value === undefined ? '' : JSON.stringify(value, null, 2);
  }

  /** The resolved node for a given property (template convenience). */
  nodeFor(parent: SchemaNode | null, key: string): SchemaNode | null {
    const resolved = this.resolve(parent);
    return this.resolve(resolved?.properties?.[key]);
  }

  /** True when a subtree needs to be rendered as a JSON textarea. */
  needsJsonFallback(node: SchemaNode | null, depth: number): boolean {
    return !this.canRender(node, depth);
  }

  trackEntry(_i: number, entry: [string, SchemaNode]): string {
    return entry[0];
  }
}

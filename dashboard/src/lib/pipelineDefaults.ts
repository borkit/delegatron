// Pipeline defaults the Settings page keeps in the browser and the New Run form reads.
//
// A value is stored only when the user changed it, and removed again when it
// equals the server default, so a later change to the server's defaults
// (/api/defaults, i.e. the DELEGATRON_* environment) still reaches the browser.
//
// Pure module: no React, no `window`. Every function takes the storage to use,
// and only erasable TypeScript syntax is used, so `node --test` runs the tests
// without a build step.

export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface PipelineDefaults {
  max_iterations: number;
  max_cost_usd: number;
  max_wall_time: number; // minutes
  sandbox: string;
  workspace: string;
}

export type PipelineField = keyof PipelineDefaults;

// The fields of /api/defaults these defaults come from.
export interface ServerPipelineDefaults {
  max_iterations: number;
  max_cost_usd: number;
  max_wall_time_minutes: number;
  sandbox: string;
  workspace: string;
}

export const PIPELINE_DEFAULT_KEYS: Record<PipelineField, string> = {
  max_iterations: "delegatron_max_iterations",
  max_cost_usd: "delegatron_max_cost_usd",
  max_wall_time: "delegatron_max_wall_time",
  sandbox: "delegatron_sandbox",
  workspace: "delegatron_workspace",
};

const FIELDS = Object.keys(PIPELINE_DEFAULT_KEYS) as PipelineField[];

// Used only when the server's defaults are unknown (request failed or pending).
export const FALLBACK_DEFAULTS: PipelineDefaults = {
  max_iterations: 5,
  max_cost_usd: 5.0,
  max_wall_time: 60,
  sandbox: "docker",
  workspace: "",
};

export const SETTINGS_VERSION_KEY = "delegatron_settings_version";
export const SETTINGS_VERSION = "2";

// Settings once saved every field, so browsers carry the old 30-minute wall time.
const LEGACY_WALL_TIME = "30";

// Drop the stale 30-minute wall time once per browser (settings version < 2).
// Idempotent: after it ran, a 30 the user saves is a deliberate choice and is kept.
export function migrateLegacyDefaults(storage: KeyValueStorage): void {
  const version = Number(storage.getItem(SETTINGS_VERSION_KEY) ?? "0");
  if (version >= Number(SETTINGS_VERSION)) return;
  if (storage.getItem(PIPELINE_DEFAULT_KEYS.max_wall_time) === LEGACY_WALL_TIME) {
    storage.removeItem(PIPELINE_DEFAULT_KEYS.max_wall_time);
  }
  storage.setItem(SETTINGS_VERSION_KEY, SETTINGS_VERSION);
}

// An in-memory storage, e.g. to migrate an imported settings file before writing it.
export function memoryStorage(
  initial: Record<string, string> = {},
): KeyValueStorage & { snapshot(): Record<string, string> } {
  const items = new Map<string, string>(Object.entries(initial));
  return {
    getItem: (key) => (items.has(key) ? (items.get(key) as string) : null),
    setItem: (key, value) => {
      items.set(key, String(value));
    },
    removeItem: (key) => {
      items.delete(key);
    },
    snapshot: () => Object.fromEntries(items),
  };
}

// The settings of an exported file, migrated when the file predates the
// current settings version (an old export must not pin the 30-minute wall time).
export function migrateImportedSettings(settings: Record<string, string>): Record<string, string> {
  if (SETTINGS_VERSION_KEY in settings) return { ...settings };
  const staged = memoryStorage(settings);
  migrateLegacyDefaults(staged);
  return staged.snapshot();
}

// The server's default for a field, as stored text (null if the server sent none).
function serverValue(server: ServerPipelineDefaults, field: PipelineField): string | null {
  const value = field === "max_wall_time" ? server.max_wall_time_minutes : server[field];
  return value === undefined || value === null ? null : String(value);
}

// Persist the fields the user changed (`dirty`); never touch the others.
// A changed field equal to the server default is removed instead, so it keeps
// following the server. With `serverDefaults` unknown, the choice is stored as is.
export function savePipelineDefaults(
  storage: KeyValueStorage,
  values: PipelineDefaults,
  dirty: Iterable<PipelineField>,
  serverDefaults: ServerPipelineDefaults | null,
): void {
  for (const field of dirty) {
    const key = PIPELINE_DEFAULT_KEYS[field];
    const value = String(values[field]);
    if (serverDefaults !== null && value === serverValue(serverDefaults, field)) {
      storage.removeItem(key);
    } else {
      storage.setItem(key, value);
    }
  }
}

// Forget every stored pipeline default; the form then shows the server's.
export function resetPipelineDefaults(storage: KeyValueStorage): void {
  for (const field of FIELDS) storage.removeItem(PIPELINE_DEFAULT_KEYS[field]);
}

function validNumber(field: PipelineField, value: string): number | null {
  if (value.trim() === "") return null;
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  // 0 means "unlimited" for the cost cap only.
  return field === "max_cost_usd" ? (n >= 0 ? n : null) : n >= 1 ? n : null;
}

function resolveField(
  field: PipelineField,
  stored: string | null,
  server: ServerPipelineDefaults | null,
): number | string {
  const candidates: Array<string | null> = [stored, server ? serverValue(server, field) : null];
  for (const candidate of candidates) {
    if (candidate === null) continue;
    if (typeof FALLBACK_DEFAULTS[field] === "number") {
      const n = validNumber(field, candidate);
      if (n !== null) return n;
    } else if (candidate !== "") {
      return candidate;
    }
  }
  return FALLBACK_DEFAULTS[field];
}

// The form's starting values: stored value > server default > FALLBACK_DEFAULTS.
export function resolveFormDefaults(
  storage: KeyValueStorage,
  serverDefaults: ServerPipelineDefaults | null,
): PipelineDefaults {
  const resolved = { ...FALLBACK_DEFAULTS };
  for (const field of FIELDS) {
    const value = resolveField(field, storage.getItem(PIPELINE_DEFAULT_KEYS[field]), serverDefaults);
    (resolved as Record<PipelineField, number | string>)[field] = value;
  }
  return resolved;
}

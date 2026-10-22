// Run with `npm test` (node --test; Node >= 23.6 strips the TypeScript types).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  FALLBACK_DEFAULTS,
  PIPELINE_DEFAULT_KEYS,
  SETTINGS_VERSION_KEY,
  memoryStorage,
  migrateImportedSettings,
  migrateLegacyDefaults,
  resetPipelineDefaults,
  resolveFormDefaults,
  savePipelineDefaults,
} from "./pipelineDefaults.ts";
import type { PipelineDefaults, ServerPipelineDefaults } from "./pipelineDefaults.ts";

const WALL = PIPELINE_DEFAULT_KEYS.max_wall_time;
const ITER = PIPELINE_DEFAULT_KEYS.max_iterations;

const SERVER: ServerPipelineDefaults = {
  max_iterations: 5,
  max_cost_usd: 5.0,
  max_wall_time_minutes: 90,
  sandbox: "e2b",
  workspace: "/srv/workspace",
};

const FORM: PipelineDefaults = {
  max_iterations: 10,
  max_cost_usd: 5,
  max_wall_time: 60,
  sandbox: "docker",
  workspace: "/srv/workspace",
};

describe("migrateLegacyDefaults", () => {
  it("removes the legacy 30-minute wall time, sets version 2, and a second call is a no-op", () => {
    const storage = memoryStorage({ [WALL]: "30", [ITER]: "10" });

    migrateLegacyDefaults(storage);
    assert.deepEqual(storage.snapshot(), { [ITER]: "10", [SETTINGS_VERSION_KEY]: "2" });

    migrateLegacyDefaults(storage);
    assert.deepEqual(storage.snapshot(), { [ITER]: "10", [SETTINGS_VERSION_KEY]: "2" });
  });

  it("keeps a non-legacy value and a 30 saved after the migration", () => {
    const kept = memoryStorage({ [WALL]: "45" });
    migrateLegacyDefaults(kept);
    assert.equal(kept.getItem(WALL), "45");

    const chosen = memoryStorage({ [SETTINGS_VERSION_KEY]: "2" });
    chosen.setItem(WALL, "30");
    migrateLegacyDefaults(chosen);
    migrateLegacyDefaults(chosen);
    assert.equal(chosen.getItem(WALL), "30");
  });

  it("migrates an imported export that predates the version key", () => {
    const imported = migrateImportedSettings({ [WALL]: "30", [ITER]: "10" });
    assert.deepEqual(imported, { [ITER]: "10", [SETTINGS_VERSION_KEY]: "2" });

    const current = migrateImportedSettings({ [WALL]: "30", [SETTINGS_VERSION_KEY]: "2" });
    assert.equal(current[WALL], "30");
  });
});

describe("savePipelineDefaults", () => {
  it("writes only the dirty keys", () => {
    const storage = memoryStorage({ [SETTINGS_VERSION_KEY]: "2" });

    savePipelineDefaults(storage, FORM, new Set(["max_iterations"] as const), SERVER);

    assert.deepEqual(storage.snapshot(), { [SETTINGS_VERSION_KEY]: "2", [ITER]: "10" });
  });

  it("removes a dirty key whose value equals the server default", () => {
    const storage = memoryStorage({ [ITER]: "10", [WALL]: "45" });

    savePipelineDefaults(
      storage,
      { ...FORM, max_iterations: 5, max_wall_time: 90 },
      ["max_iterations", "max_wall_time"],
      SERVER,
    );

    assert.deepEqual(storage.snapshot(), {});
  });

  it("stores dirty keys as chosen when the server defaults are unknown, and leaves the rest", () => {
    const storage = memoryStorage({ [PIPELINE_DEFAULT_KEYS.max_cost_usd]: "13" });

    savePipelineDefaults(storage, FORM, ["max_wall_time", "sandbox"], null);

    assert.deepEqual(storage.snapshot(), {
      [PIPELINE_DEFAULT_KEYS.max_cost_usd]: "13",
      [WALL]: "60",
      [PIPELINE_DEFAULT_KEYS.sandbox]: "docker",
    });
  });
});

describe("resolveFormDefaults", () => {
  it("prefers stored values, then the server defaults, then the fallbacks", () => {
    const storage = memoryStorage({ [ITER]: "12", [PIPELINE_DEFAULT_KEYS.max_cost_usd]: "0" });

    assert.deepEqual(resolveFormDefaults(storage, SERVER), {
      max_iterations: 12,
      max_cost_usd: 0,
      max_wall_time: 90,
      sandbox: "e2b",
      workspace: "/srv/workspace",
    });
    assert.deepEqual(resolveFormDefaults(memoryStorage(), null), FALLBACK_DEFAULTS);
    assert.equal(FALLBACK_DEFAULTS.max_wall_time, 60);
  });

  it("ignores unusable stored or server values", () => {
    const storage = memoryStorage({ [ITER]: "abc", [WALL]: "0", [PIPELINE_DEFAULT_KEYS.sandbox]: "" });
    const partialServer = { ...SERVER, max_wall_time_minutes: undefined } as unknown as ServerPipelineDefaults;

    const resolved = resolveFormDefaults(storage, partialServer);

    assert.equal(resolved.max_iterations, 5);
    assert.equal(resolved.max_wall_time, 60);
    assert.equal(resolved.sandbox, "e2b");
  });
});

describe("resetPipelineDefaults", () => {
  it("removes the five pipeline keys and nothing else", () => {
    const storage = memoryStorage({
      ...Object.fromEntries(Object.values(PIPELINE_DEFAULT_KEYS).map((key) => [key, "1"])),
      delegatron_preset: "local",
      [SETTINGS_VERSION_KEY]: "2",
    });

    resetPipelineDefaults(storage);

    assert.deepEqual(storage.snapshot(), { delegatron_preset: "local", [SETTINGS_VERSION_KEY]: "2" });
    assert.deepEqual(resolveFormDefaults(storage, SERVER).max_wall_time, 90);
  });
});

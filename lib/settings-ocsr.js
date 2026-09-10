/**
 * OCSR dsh adapter — Phase 3 settings namespace + white-list gate.
 *
 * Two responsibilities:
 *  1. Register the `dsh-ocsr` settings namespace with the host
 *     `ctx.get('settings')` service (an optional service — if unavailable the
 *     plugin skips registration and warns instead of failing).
 *  2. Export pure helpers used by lib/tool-ocsr.js for the model gate:
 *     `loadDriverBaseModels()` (the only JS reader of the driver's
 *     config/allowed-models.json), `parseWorkerModel(worker)` (the driver's
 *     `PROMPT_PATH|MODEL|LABEL` split('|',2) mapping), and
 *     `effectiveAllowedModels(driverBaseModels, userOverride)`.
 *
 * This is a configuration gate only; it never copies OCSR dispatch semantics.
 * The white list can only be narrowed (intersection), never widened.
 *
 * @module @ocsr/dsh-ocsr/settings
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import z from "@deepseek-ai/schemastery";

/** Absolute path of the driver's allowed-models file (same as the Python side). */
const ALLOWED_MODELS_PATH = fileURLToPath(
  new URL("../config/allowed-models.json", import.meta.url),
);

/** Cordis plugin name (the id matched in cordis.patch.yml). */
const name = "ocsr-settings";
/** Optional service retrieved at runtime via `ctx.get('settings')`. */
const inject = [];

/** Named logger used by the pure helpers (may be reassigned by apply). */
let diagnosticWarn = (message) => console.warn(message);

/** Set the structured logger used for warnings (Cordis ctx.logger). */
function setDiagnosticLogger(logger) {
  diagnosticWarn = (message) => logger?.warn?.(message);
}

function warn(message) {
  diagnosticWarn(message);
}

/**
 * Read the driver's allowed-models file and return the configured models.
 * Missing file, invalid JSON, an empty array, non-`provider/model` strings, or
 * duplicate entries all fail closed to [] (with a warning) so the gate never
 * silently widens the driver's own ALLOWED_MODELS.
 */
function loadDriverBaseModels() {
  try {
    const raw = readFileSync(ALLOWED_MODELS_PATH, "utf8");
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      warn(`ocsr-settings: allowed-models.json is not valid JSON; fail-closed: ${String(error?.message ?? error)}`);
      return [];
    }
    if (!Array.isArray(parsed) || parsed.length === 0) {
      warn("ocsr-settings: allowed-models.json is empty or not an array; fail-closed");
      return [];
    }
    const invalid = parsed.some((m) => {
      if (typeof m !== "string") return true;
      const parts = m.split("/");
      return parts.length !== 2 || !parts[0] || !parts[1];
    });
    if (invalid) {
      warn("ocsr-settings: allowed-models.json contains non-provider/model entries; fail-closed");
      return [];
    }
    const unique = [...new Set(parsed)];
    if (unique.length !== parsed.length) {
      warn("ocsr-settings: allowed-models.json contains duplicates; fail-closed");
      return [];
    }
    return unique;
  } catch (error) {
    warn(`ocsr-settings: cannot read allowed-models.json; fail-closed: ${String(error?.message ?? error)}`);
    return [];
  }
}

/**
 * Parse the driver's `PROMPT_PATH|MODEL|LABEL` format (Python
 * `split("|", 2)`, maxsplit=2) and return the model segment, or null when the
 * worker is not a valid three-segment worker string.
 */
function parseWorkerModel(worker) {
  if (typeof worker !== "string" || worker.length === 0) return null;
  const first = worker.indexOf("|");
  if (first < 0) return null;
  const second = worker.indexOf("|", first + 1);
  if (second < 0) return null;
  const model = worker.slice(first + 1, second);
  if (model.length === 0) return null;
  return model;
}

/**
 * Intersect the driver's base white list with the dsh settings override. The
 * override can only narrow (or close) the white list; it can never widen it.
 * - undefined override -> driver base unchanged.
 * - empty array -> explicit close (allow nothing).
 * - non-empty array -> intersection (narrow).
 * - non-array override -> fail closed (allow nothing).
 */
function effectiveAllowedModels(driverBaseModels, userOverride) {
  if (userOverride === undefined) return driverBaseModels;
  if (!Array.isArray(userOverride)) return [];
  if (userOverride.length === 0) return [];
  return driverBaseModels.filter((model) => userOverride.includes(model));
}

/** Register the `dsh-ocsr` settings namespace on `ctx.get('settings')`. */
function apply(ctx) {
  const settings = ctx.get("settings");
  if (!settings) {
    ctx.logger?.warn?.("ocsr-settings: settings unavailable, skip registration");
    return;
  }
  setDiagnosticLogger(ctx.logger);
  settings.register(
    "dsh-ocsr",
    z.object({
      allowedModels: z.array(z.string()),
    }),
    { base: ctx.config },
  );
}

export {
  apply,
  effectiveAllowedModels,
  inject,
  loadDriverBaseModels,
  name,
  parseWorkerModel,
};

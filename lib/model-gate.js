/**
 * OCSR dsh adapter — model-allowlist helpers for the `ocsr_dispatch` tool.
 *
 * These are pure helpers only. The user-configurable narrowing gate is the
 * `allowedModels` field of this plugin's own Config schema (see
 * `lib/tool-ocsr.js`), which the host validates and which is set through the
 * `ocsr-tools` entry's patch config; the *base* allowlist stays owned by the
 * Python driver's user-local file so the JS layer never invents a second source
 * of truth.
 *
 * The base file is resolved with exactly the driver's precedence
 * (`OCSR_ALLOWED_MODELS_PATH` > `OCSR_HOME/allowed-models.json` >
 * `~/.ocsr/allowed-models.json`), so a model the driver would reject can never
 * pass this gate by pointing at a different file.
 *
 * @module @ocsr/dsh-ocsr/model-gate
 */
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** Named logger used by the pure helpers (reassigned by the tool plugin). */
let diagnosticWarn = (message) => console.warn(message);

/** Set the structured logger used for warnings (Cordis ctx.logger). */
function setDiagnosticLogger(logger) {
  diagnosticWarn = (message) => logger?.warn?.(message);
}

/** Messages already reported, so an unconfigured allowlist warns once per process. */
const reported = new Set();

function warn(message) {
  if (reported.has(message)) return;
  reported.add(message);
  diagnosticWarn(message);
}

/**
 * Resolve the driver's allowlist path, mirroring `scripts/ocsr_dispatch.py`
 * (`OCSR_ALLOWED_MODELS_PATH` > `OCSR_HOME/allowed-models.json` >
 * `~/.ocsr/allowed-models.json`).
 */
function allowedModelsPath() {
  const explicit = process.env.OCSR_ALLOWED_MODELS_PATH;
  if (explicit) return explicit;
  const home = process.env.OCSR_HOME || join(homedir(), ".ocsr");
  return join(home, "allowed-models.json");
}

/**
 * Read the driver's user-local allowlist and return the configured models.
 * Missing file or empty array means "unconfigured" and closes the gate to [].
 * Invalid JSON, non-`provider/model` strings, surrounding whitespace, or
 * duplicate entries also fail closed to [] (with a warning), so the gate never
 * silently widens the driver's own ALLOWED_MODELS.
 */
function loadDriverBaseModels(path = allowedModelsPath()) {
  try {
    const raw = readFileSync(path, "utf8");
    if (raw.trim() === "") {
      warn(`ocsr: allowed-models.json is empty; fail-closed (${path})`);
      return [];
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      warn(`ocsr: allowed-models.json is not valid JSON; fail-closed: ${String(error?.message ?? error)}`);
      return [];
    }
    if (!Array.isArray(parsed) || parsed.length === 0) {
      warn("ocsr: allowed-models.json is empty or not an array; fail-closed");
      return [];
    }
    const invalid = parsed.some((model) => {
      if (typeof model !== "string") return true;
      const parts = model.split("/");
      if (parts.length !== 2 || !parts[0] || !parts[1]) return true;
      return parts.some((part) => part !== part.trim());
    });
    if (invalid) {
      warn("ocsr: allowed-models.json contains non-provider/model entries; fail-closed");
      return [];
    }
    const unique = [...new Set(parsed)];
    if (unique.length !== parsed.length) {
      warn("ocsr: allowed-models.json contains duplicates; fail-closed");
      return [];
    }
    return unique;
  } catch (error) {
    warn(`ocsr: cannot read allowed-models.json; fail-closed: ${String(error?.message ?? error)}`);
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
 * Intersect the driver's base allowlist with the plugin Config override. The
 * override can only narrow (or close) the allowlist; it can never widen it.
 * - undefined override -> driver base unchanged (field omitted).
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

export {
  allowedModelsPath,
  effectiveAllowedModels,
  loadDriverBaseModels,
  parseWorkerModel,
  setDiagnosticLogger,
};

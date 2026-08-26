/**
 * OCSR dsh adapter — Phase 2/3 first-class `ocsr_dispatch` tool.
 *
 * A thin Cordis tool plugin that fronts `scripts/ocsr_dispatch.py dispatch` on
 * the host-plane `ctx.tools` registry. It maps validated schema arguments onto
 * the Python driver's CLI and normalizes the result; all orchestration
 * semantics (parallel workers, watch, timeout policy, ledger) live in the
 * Python driver, never in this module.
 *
 * Phase 3 adds `background`: when true the child process is registered as a
 * host-plane background job (via `ctx.get('jobs')`) instead of being awaited
 * in the foreground, and a model gate (from ./settings-ocsr.js) rejects any
 * worker whose parsed model is not currently allowed.
 *
 * @module @ocsr/dsh-ocsr/tool
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { defineTool } from "@deepseek-ai/dsh-tools";
import {
  effectiveAllowedModels,
  loadDriverBaseModels,
  parseWorkerModel,
} from "./settings-ocsr.js";

/** Absolute directory of the packaged Python driver. */
const SCRIPTS = fileURLToPath(new URL("../scripts/", import.meta.url));
/** Absolute path of the OCSR dispatch driver. */
const DRIVER = SCRIPTS + "ocsr_dispatch.py";
/** Package root used as the child process cwd. */
const PACKAGE_ROOT = fileURLToPath(new URL("../", import.meta.url));

/** Maximum characters of each stream shown by the model-facing renderer. */
const OUTPUT_LIMIT = 4000;

/** Cordis plugin name (the id matched in cordis.patch.yml). */
const name = "ocsr-tools";
/** Service required by the host-plane tool registry. */
const inject = ["tools"];

/** Trim stdout/stderr to a bound with a truncation notice. */
function cap(text) {
  const s = String(text ?? "");
  if (s.length <= OUTPUT_LIMIT) return s;
  return `${s.slice(0, OUTPUT_LIMIT)}
[truncated: ${s.length} characters]`;
}

/**
 * Render a normalized dispatch result for the model. Background results are
 * rendered first so that even failed background starts never fall through to
 * the foreground renderer (which would otherwise print `command: undefined`).
 */
function renderResult(value) {
  if (value.kind === "background") {
    if (value.ok) {
      return `[background job] jobId=${value.jobId} (kind=ocsr-dispatch)`;
    }
    return `[background job] failed: ${value.stderr ?? "unknown"}`;
  }
  const parts = [];
  if (value.stdout && value.stdout.length > 0) parts.push(cap(value.stdout));
  if (value.stderr && value.stderr.length > 0) {
    parts.push(`[stderr]
${cap(value.stderr)}`);
  }
  if (parts.length === 0) parts.push("(no output)");
  parts.push(`[ok: ${value.ok}] [exit code: ${value.code}]`);
  if (value.command !== undefined) parts.push(`command: ${value.command}`);
  return parts.join("\n");
}

/** Build the driver argv from validated schema arguments. */
function buildArgv(args) {
  const argv = [
    DRIVER,
    "dispatch",
    "--worker",
    String(args.worker),
    "--output-dir",
    String(args.output_dir),
  ];
  if (args.output_pattern !== undefined) argv.push("--output-pattern", String(args.output_pattern));
  if (args.watch === true) argv.push("--watch");
  if (args.timeout !== undefined) argv.push("--timeout", String(args.timeout));
  if (args.timeout_policy !== undefined) argv.push("--timeout-policy", String(args.timeout_policy));
  if (args.stagger !== undefined) argv.push("--stagger", String(args.stagger));
  if (args.harness !== undefined) argv.push("--harness", String(args.harness));
  if (args.progress === true) argv.push("--progress");
  for (const p of args.forbid_paths ?? []) argv.push("--forbid-paths", String(p));
  for (const m of args.meta ?? []) argv.push("--meta", String(m));
  return argv;
}

/** Abort the child process and reject as an OS-signal cancellation. */
function abortError() {
  const error = new Error("tool call aborted");
  error.name = "AbortError";
  return error;
}

/**
 * Run the Python driver via `node <script>`, capturing stdout/stderr and
 * honoring `exec.signal` cancellation.
 */
function runDriver(argv, exec) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, argv, {
      cwd: PACKAGE_ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdoutChunks = [];
    const stderrChunks = [];
    child.stdout.on("data", (chunk) => stdoutChunks.push(chunk));
    child.stderr.on("data", (chunk) => stderrChunks.push(chunk));

    const onAbort = () => child.kill();
    const cleanup = () => exec.signal?.removeEventListener("abort", onAbort);

    if (exec.signal?.aborted) {
      cleanup();
      child.kill();
      reject(abortError());
      return;
    }
    exec.signal?.addEventListener("abort", onAbort, { once: true });

    child.on("error", (error) => {
      cleanup();
      resolve({
        ok: false,
        command: argv.join(" "),
        stdout: Buffer.concat(stdoutChunks).toString("utf8"),
        stderr: String(error.message),
        code: 1,
      });
    });

    child.on("close", (code, signal) => {
      cleanup();
      if (exec.signal?.aborted) {
        reject(abortError());
        return;
      }
      resolve({
        ok: code === 0,
        command: argv.join(" "),
        stdout: Buffer.concat(stdoutChunks).toString("utf8"),
        stderr: Buffer.concat(stderrChunks).toString("utf8"),
        code: code ?? (signal ? 1 : 0),
      });
    });
  });
}

/**
 * Build the `{ cancel, done, readOutput }` handle returned by a background
 * job's `run`. Mirrors the dsh-tool-bash producer contract:
 * `readOutput` drains the child's combined output and returns the delta since
 * the previous read (the caller accumulates deltas to get the full stream).
 */
function buildBackgroundSpec(argv, exec) {
  const child = spawn(process.execPath, argv, {
    cwd: PACKAGE_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    signal: exec.signal,
  });
  let combined = "";
  let readPos = 0;
  let stderr = "";
  let killedByCancel = false;

  child.stdout.on("data", (chunk) => {
    combined += chunk.toString("utf8");
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString("utf8");
    combined += chunk.toString("utf8");
  });

  const cancel = () => {
    killedByCancel = true;
    child.kill();
  };
  const onAbort = () => cancel();
  if (exec.signal?.aborted) cancel();
  exec.signal?.addEventListener("abort", onAbort, { once: true });

  const done = new Promise((resolve) => {
    const cleanup = () => exec.signal?.removeEventListener("abort", onAbort);
    child.on("error", (error) => {
      cleanup();
      resolve({
        status: killedByCancel ? "killed" : "completed",
        detail: String(error.message),
        output: combined,
      });
    });
    child.on("close", (code, signal) => {
      cleanup();
      const detail = killedByCancel
        ? signal
          ? `killed by signal ${signal}`
          : "killed"
        : stderr.trim()
          ? stderr
          : undefined;
      resolve({
        status: killedByCancel ? "killed" : "completed",
        detail,
        output: combined,
      });
    });
  });

  return {
    cancel,
    done,
    readOutput: () => {
      const delta = combined.slice(readPos);
      readPos = combined.length;
      return delta;
    },
  };
}

/** Register the model-facing `ocsr_dispatch` tool on `ctx.tools`. */
function apply(ctx) {
  ctx.tools.register(
    defineTool({
      name: "ocsr_dispatch",
      description:
        "Dispatch an OCSR worker run through the OCSR headless driver (`scripts/ocsr_dispatch.py dispatch`). Use this to launch cross-vendor or fresh-context model workers in parallel and collect their results. All orchestration (workers, watch, timeout policy, ledger) is handled by the Python driver; this tool only forwards validated arguments.",
      parameters: {
        worker: {
          type: "string",
          required: true,
          description:
            "The worker in driver format `PROMPT_PATH|MODEL|LABEL`.",
        },
        output_dir: {
          type: "string",
          required: true,
          description:
            "Directory where the driver writes per-worker output.",
        },
        output_pattern: {
          type: "string",
          description:
            "Output filename pattern containing `{date}`, `{label}`, `{model}` tokens. Defaults to the driver default.",
        },
        background: {
          type: "boolean",
          default: false,
          description:
            "Run the dispatch as a dsh background job and return a jobId instead of waiting for completion in the foreground. Requires the host to mount dsh jobs services.",
        },
        watch: {
          type: "boolean",
          description:
            "Keep watching for new output after all workers finish.",
        },
        timeout: {
          type: "number",
          description:
            "Per-worker timeout in minutes. Defaults to 15 in the driver.",
        },
        timeout_policy: {
          type: "string",
          enum: ["auto", "hierarchical_report", "leaf_kill"],
          description:
            "What to do when a worker exceeds `timeout`: `auto`, `hierarchical_report`, or `leaf_kill`.",
        },
        stagger: {
          type: "number",
          description:
            "Delay in seconds between starting consecutive workers. Defaults to 5 in the driver.",
        },
        harness: {
          type: "string",
          description:
            "Harness used for the workers (default `cli`).",
        },
        forbid_paths: {
          type: "array",
          items: { type: "string" },
          description:
            "Paths the dispatched workers must not touch; repeat for each path.",
        },
        meta: {
          type: "array",
          items: { type: "string" },
          description:
            "Extra `KEY=VALUE` metadata to record in the ledger; repeat for each entry.",
        },
        progress: {
          type: "boolean",
          description:
            "Show live progress output while dispatching.",
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            ok: { type: "boolean", required: true },
            kind: { type: "string", enum: ["foreground", "background"] },
            jobId: {
              oneOf: [{ type: "string" }, { type: "null" }],
            },
            command: { type: "string" },
            code: { type: "number" },
            stdout: { type: "string" },
            stderr: { type: "string" },
          },
        },
        render: (_args, value) => [
          { type: "text", text: renderResult(value) },
        ],
      },
      async execute(args, exec) {
        // Model gate runs on every call (worker is required) before either the
        // foreground or background path. ctx.get('settings') must be null-guarded
        // because ctx.settings (property accessor) throws when the service is
        // unavailable.
        const settings = ctx.get("settings");
        const userOverride = settings?.get?.("dsh-ocsr")?.allowedModels;
        const driverBaseModels = loadDriverBaseModels();
        const model = parseWorkerModel(String(args.worker));
        if (
          model === null ||
          !effectiveAllowedModels(driverBaseModels, userOverride).includes(model)
        ) {
          return {
            ok: false,
            kind: args.background ? "background" : "foreground",
            jobId: null,
            code: 3,
            stderr: "model not allowed / unparsable worker",
          };
        }

        if (args.background === true) {
          const jobs = ctx.get("jobs");
          if (!jobs) {
            return {
              ok: false,
              kind: "background",
              jobId: null,
              code: 1,
              stderr:
                "background jobs unavailable: load @deepseek-ai/dsh-jobs + dsh-tool-jobs",
            };
          }
          try {
            const jobId = jobs.start({
              kind: "ocsr-dispatch",
              label: `${String(args.worker)} -> ${String(args.output_dir)}`,
              owner: exec.agent,
              run: () => buildBackgroundSpec(buildArgv(args), exec),
            });
            return { ok: true, kind: "background", jobId };
          } catch (e) {
            return {
              ok: false,
              kind: "background",
              jobId: null,
              code: 2,
              stderr: String(e?.message ?? e),
            };
          }
        }

        const result = await runDriver(buildArgv(args), exec);
        return { ...result, kind: "foreground", jobId: null };
      },
    }),
  );
}

export { apply, inject, name };

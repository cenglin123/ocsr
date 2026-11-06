/**
 * OCSR dsh adapter — first-class `ocsr_dispatch` tool.
 *
 * A thin Cordis tool plugin that fronts `scripts/ocsr_dispatch.py dispatch` on
 * the host-plane `ctx.tools` registry. It maps validated schema arguments onto
 * the Python driver's CLI and normalizes the result; all orchestration
 * semantics (parallel workers, watch, timeout policy, ledger) live in the
 * Python driver, never in this module.
 *
 * `background: true` registers the child process as a host-plane background
 * job through `ctx.get('jobs')`; the durable output ring is fed with
 * `JobHandle.append`, so `job_output` / `job_kill` see the same stream the
 * foreground path prints.
 *
 * The optional `Config.allowedModels` field is a narrowing-only gate on top of
 * the driver's user-local allowlist (see ./model-gate.js). It is ordinary
 * plugin Config, so the host validates it and it can be set through the
 * `ocsr-tools` entry's patch config; the gate can only narrow or close, never
 * widen, the driver's own list.
 *
 * @module @ocsr/dsh-ocsr/tool
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { defineTool } from "@deepseek-ai/dsh-tools";
import z from "@deepseek-ai/schemastery";
import {
  effectiveAllowedModels,
  loadDriverBaseModels,
  parseWorkerModel,
  setDiagnosticLogger,
} from "./model-gate.js";

/** Absolute directory of the packaged Python driver. */
const SCRIPTS = fileURLToPath(new URL("../scripts/", import.meta.url));
/** Absolute path of the OCSR dispatch driver. */
const DRIVER = SCRIPTS + "ocsr_dispatch.py";
/** Package root used as the child process cwd. */
const PACKAGE_ROOT = fileURLToPath(new URL("../", import.meta.url));
/** Python interpreter used to run the driver (OCSR_PYTHON overrides). */
const PYTHON = process.env.OCSR_PYTHON || "python";

/**
 * Tool-local error codes. Deliberately outside the driver's exit-code
 * contract (0=ok, 1=watchdog timeout, 2=deterministic failure, 3=path
 * collision) so callers never confuse a tool-level rejection with a driver
 * outcome. Driver subprocess exit codes are passed through unchanged.
 */
const TOOL_CODE_REJECTED = 4;
const TOOL_CODE_INFRA = 5;

/** Maximum characters of each stream shown by the model-facing renderer. */
const OUTPUT_LIMIT = 4000;

/** Cordis plugin name (the id matched in cordis.patch.yml). */
const name = "ocsr-tools";
/** Service required by the host-plane tool registry. */
const inject = ["tools"];

/**
 * Plugin Config: an optional narrow-only model allowlist.
 *
 * Omitted (the default) -> the driver's user-local allowlist decides alone.
 * Explicit empty array -> close the gate entirely.
 * Non-empty -> intersection with the driver list (never a substitution, so a
 * mis-set value cannot widen what the driver accepts). Configure it through the
 * `ocsr-tools` entry's `config` in the profile patch: the field is not volatile,
 * so it is not projected as an editable settings-form field.
 *
 * `.default(undefined)` is load-bearing: without it schemastery fills an omitted
 * array field with `[]`, which would silently close the gate for every user who
 * never configured anything, instead of leaving the driver in charge.
 */
const Config = z.object({
  allowedModels: z.array(z.string()).default(undefined),
});

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
  if (args.total_timeout !== undefined) argv.push("--total-timeout", String(args.total_timeout));
  if (args.renewal_minutes !== undefined) argv.push("--renewal-minutes", String(args.renewal_minutes));
  if (args.max_renewals !== undefined) argv.push("--max-renewals", String(args.max_renewals));
  if (args.attempt_index !== undefined) argv.push("--attempt-index", String(args.attempt_index));
  if (args.recovery_safe === true) argv.push("--recovery-safe");
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
 * Run the Python driver via the Python interpreter (`OCSR_PYTHON`, default
 * `python`), capturing stdout/stderr and honoring `exec.signal` cancellation.
 */
function runDriver(argv, exec) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, argv, {
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
        command: [PYTHON, ...argv].join(" "),
        stdout: Buffer.concat(stdoutChunks).toString("utf8"),
        stderr: String(error.message),
        code: TOOL_CODE_INFRA,
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
 * First non-empty line of `text`, trimmed and bounded, for contract fields that
 * are rendered as one line (`JobOutcome.detail`).
 */
function singleLine(text, limit = 400) {
  const line = String(text ?? "")
    .split(/\r?\n/)
    .find((part) => part.trim().length > 0);
  return (line ?? "").trim().slice(0, limit);
}

/**
 * Build the host-plane background job spec for one dispatch.
 *
 * The producer contract hands `run(job)` a `JobHandle`, and everything the
 * model later reads back through `job_output` must be appended to the
 * registry-owned output ring; a producer's own buffer is invisible. The
 * returned hooks are the whole `JobHooks` contract: a synchronous
 * `cancel(reason?)` and a `done` promise resolving `{ status, detail }`, where
 * `status` separates completion, cancellation, and failure.
 *
 * A background job outlives the tool call that started it: it is owned by the
 * session, and the registry is the only thing allowed to stop it (`job_kill`
 * reaches `cancel`). The turn's abort signal is therefore deliberately NOT
 * forwarded to the child, so cancelling a model turn does not silently kill a
 * dispatch the user was told to collect through `job_output`.
 */
function buildBackgroundSpec(job, argv) {
  const child = spawn(PYTHON, argv, {
    cwd: PACKAGE_ROOT,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderrText = "";
  let killedByCancel = false;
  let killReason = "";

  // After settlement the ring drops late writes instead of throwing; the guard
  // only protects against a registry that throws during teardown.
  const append = (text, options) => {
    if (text.length === 0) return;
    try {
      job.append(text, options);
    } catch {
      /* output ring closed; nothing left to report */
    }
  };

  child.stdout.on("data", (chunk) => append(chunk.toString("utf8")));
  child.stderr.on("data", (chunk) => {
    const text = chunk.toString("utf8");
    stderrText += text;
    append(text, { channel: "stderr" });
  });

  const cancel = (reason) => {
    killedByCancel = true;
    killReason = singleLine(reason);
    child.kill();
  };

  const done = new Promise((resolve) => {
    child.on("error", (error) => {
      resolve({
        status: killedByCancel ? "killed" : "failed",
        detail: killedByCancel ? killReason || "killed" : singleLine(error?.message ?? error),
      });
    });
    child.on("close", (code, signal) => {
      if (killedByCancel) {
        resolve({
          status: "killed",
          detail: killReason || (signal ? `killed by signal ${signal}` : "killed"),
        });
        return;
      }
      if (code === 0) {
        resolve({ status: "completed" });
        return;
      }
      resolve({
        status: "failed",
        detail: singleLine(stderrText) || `exit code: ${code ?? String(signal)}`,
      });
    });
  });

  return { cancel, done };
}

/**
 * Register the model-facing `ocsr_dispatch` tool on `ctx.tools`.
 *
 * `config` is the host-resolved plugin Config (Cordis passes it as the second
 * apply argument); `allowedModels` is captured once per activation and can only
 * narrow the allowlist the Python driver owns.
 */
function apply(ctx, config) {
  setDiagnosticLogger(ctx.logger);
  const configuredModels = config?.allowedModels;
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
          type: "integer",
          description:
            "Per-worker timeout in minutes. Defaults to 15 in the driver.",
        },
        total_timeout: {
          type: "integer",
          description: "Immutable per-worker total deadline in minutes.",
        },
        renewal_minutes: {
          type: "integer",
          description: "Minutes added after an inspection finds auditable progress.",
        },
        max_renewals: {
          type: "integer",
          description: "Finite inspection renewal count. Defaults to 1 in the driver.",
        },
        attempt_index: {
          type: "integer",
          description: "Attempt number for the same task, from 1 through 3.",
        },
        recovery_safe: {
          type: "boolean",
          description: "Assert that the worker is side-effect free or proven idempotent before resume material can be eligible.",
        },
        timeout_policy: {
          type: "string",
          enum: ["auto", "hierarchical_report", "leaf_kill"],
          description:
            "What to do when a worker exceeds `timeout`: `auto`, `hierarchical_report`, or `leaf_kill`.",
        },
        stagger: {
          type: "integer",
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
        // foreground or background path. The base allowlist is read from the
        // driver's own user-local file; the plugin Config can only narrow it.
        const driverBaseModels = loadDriverBaseModels();
        const model = parseWorkerModel(String(args.worker));
        if (
          model === null ||
          !effectiveAllowedModels(driverBaseModels, configuredModels).includes(model)
        ) {
          return {
            ok: false,
            kind: args.background ? "background" : "foreground",
            jobId: null,
            code: TOOL_CODE_REJECTED,
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
              code: TOOL_CODE_INFRA,
              stderr:
                "background jobs unavailable: load @deepseek-ai/dsh-jobs + dsh-tool-jobs",
            };
          }
          try {
            const spec = {
              kind: "ocsr-dispatch",
              label: `${String(args.worker)} -> ${String(args.output_dir)}`,
              run: (job) => buildBackgroundSpec(job, buildArgv(args)),
            };
            // Job ownership is a SessionId in dsh >= 0.2; an absent agent leaves
            // the job unowned rather than passing a non-session value.
            const owner = exec.agent?.id;
            if (owner !== undefined) spec.owner = owner;
            // Durable lifecycle records keep the creating tool invocation.
            if (exec.callId !== undefined && exec.rootCallId !== undefined) {
              spec.toolCall = { callId: exec.callId, rootCallId: exec.rootCallId };
            }
            const jobId = jobs.start(spec);
            return { ok: true, kind: "background", jobId };
          } catch (e) {
            return {
              ok: false,
              kind: "background",
              jobId: null,
              code: TOOL_CODE_INFRA,
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

export { Config, apply, inject, name };

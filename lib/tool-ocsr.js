/**
 * OCSR dsh adapter — Phase 2 first-class `ocsr_dispatch` tool.
 *
 * A thin Cordis tool plugin that fronts `scripts/ocsr_dispatch.py dispatch` on
 * the host-plane `ctx.tools` registry. It only maps validated schema arguments
 * onto the Python driver's CLI and normalizes the result; all orchestration
 * semantics (parallel workers, watch, timeout policy, ledger) live in the
 * Python driver, never in this module.
 *
 * @module @ocsr/dsh-ocsr/tool
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { defineTool } from "@deepseek-ai/dsh-tools";

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

/** Render a normalized dispatch result for the model. */
function renderResult(value) {
  const parts = [];
  if (value.stdout && value.stdout.length > 0) parts.push(cap(value.stdout));
  if (value.stderr && value.stderr.length > 0) {
    parts.push(`[stderr]
${cap(value.stderr)}`);
  }
  if (parts.length === 0) parts.push("(no output)");
  parts.push(`[ok: ${value.ok}] [exit code: ${value.code}]`);
  parts.push(`command: ${value.command}`);
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
            "The worker to run: a prompt file path, an `opencode -m` model id, or a `LABEL=|...` label pattern.",
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
            command: { type: "string", required: true },
            code: { type: "number", required: true },
            stdout: { type: "string", required: true },
            stderr: { type: "string", required: true },
          },
        },
        render: (_args, value) => [
          { type: "text", text: renderResult(value) },
        ],
      },
      async execute(args, exec) {
        return runDriver(buildArgv(args), exec);
      },
    }),
  );
}

export { apply, inject, name };

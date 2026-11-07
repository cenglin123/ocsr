/**
 * Regression tests for the OCSR ↔ dsh adapter layer.
 *
 * These run the repository's own ES modules against a *real* local dsh >= 0.2
 * runtime (see host.mjs). They exist because the adapter is pure host-contract
 * glue: the interesting failures are host API drift (removed service methods,
 * renamed fields, changed producer contracts), which no Python test can see and
 * which a hand-written fake would not catch.
 *
 * Run with `node --test tests/adapter/*.test.mjs` (Node 25 rejects a directory
 * argument), or through the pytest bridge.
 * Without a local runtime the suite reports `OCSR-ADAPTER-SKIP` and does not
 * fail; set `DSH_RUNTIME_ROOT` to point at one explicitly.
 */
import test, { after } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { REPO_ROOT, makeSandbox } from "./host.mjs";

const python = process.env.OCSR_TEST_PYTHON || process.env.OCSR_PYTHON || "python";
const pythonProbe = spawnSync(python, ["--version"], { encoding: "utf8" });
const sandbox = makeSandbox();
const requireAdapterTests = process.env.OCSR_REQUIRE_ADAPTER_TESTS === "1";
const skipReason =
  sandbox === null
    ? "no local dsh >= 0.2.2 runtime found (set DSH_RUNTIME_ROOT)"
    : pythonProbe.error || pythonProbe.status !== 0
      ? `python interpreter ${JSON.stringify(python)} is unavailable`
      : null;

if (skipReason !== null) {
  console.log(`OCSR-ADAPTER-SKIP: ${skipReason}`);
  // A runtime-less machine must still be able to verify the adapter: opt in to
  // a hard failure so CI cannot pass by skipping.
  const title = requireAdapterTests
    ? "adapter contract suite (required)"
    : "adapter contract suite (skipped)";
  test(title, { skip: requireAdapterTests ? false : skipReason }, () => {
    assert.fail(skipReason);
  });
} else {
  after(() => sandbox.cleanup());

  const importHost = (relative) =>
    import(pathToFileURL(join(sandbox.host.packagesRoot, ...relative.split("/"))).href);
  const importAdapter = (file) => import(pathToFileURL(join(sandbox.dir, "lib", file)).href);

  // The tool reads OCSR_PYTHON once at import time; the sandbox ships a fake
  // dispatcher so no test ever reaches OpenCode or a real worker directory.
  process.env.OCSR_PYTHON = python;
  const allowlistFile = join(sandbox.dir, "allowed-models.json");
  process.env.OCSR_ALLOWED_MODELS_PATH = allowlistFile;
  writeFileSync(allowlistFile, JSON.stringify(["vendor/model"]));

  const skillMod = await importAdapter("index.js");
  const toolMod = await importAdapter("tool-ocsr.js");
  const gate = await importAdapter("model-gate.js");
  const tools = await importHost("@deepseek-ai/dsh-tools/lib/index.js");
  const appBoot = await importHost("@deepseek-ai/dsh-app-boot/lib/index.js");
  const { Context } = await importHost("@deepseek-ai/cordis/lib/index.js");
  const { LocalJobRegistry } = await importHost("@deepseek-ai/dsh-jobs-local/lib/index.js");

  const OUT_DIR = join(sandbox.dir, "out");

  /** Resolve a raw Config the way the host does before calling `apply`. */
  function resolveConfig(config) {
    return toolMod.Config === undefined ? config : toolMod.Config(config);
  }

  /** Register the tool through a stub host ctx and return the tool definition. */
  function registerTool(config, services = {}) {
    let definition;
    toolMod.apply(
      {
        tools: {
          register(value) {
            definition = value;
            return () => {};
          },
        },
        get: (name) => services[name],
        logger: { warn: () => {} },
      },
      resolveConfig(config),
    );
    return definition;
  }

  function execFor(agent) {
    return { signal: new AbortController().signal, ...(agent === undefined ? {} : { agent }) };
  }

  const TERMINAL = new Set(["completed", "killed", "failed"]);

  async function waitForJob(jobs, id, caller) {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      const view = jobs.get(id, caller);
      if (TERMINAL.has(view.status)) return view;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error(`job ${id} did not settle`);
  }

  /** A bare Cordis context with a live-ish owning agent and a job controller. */
  async function createJobsContext() {
    const ctx = new Context();
    const agent = { id: "sess-1", session: { id: "sess-1" }, ctx };
    ctx.provide("agents", { get: (id) => (id === "sess-1" ? agent : undefined) });
    await ctx.plugin(LocalJobRegistry, {});
    ctx.jobs.attachController("adapter-test");
    return { ctx, agent };
  }

  test("adapter modules load against the host runtime", () => {
    assert.equal(typeof skillMod.apply, "function");
    assert.equal(skillMod.name, "ocsr-skill");
    assert.deepEqual(skillMod.inject, ["skills"]);
    assert.equal(typeof toolMod.apply, "function");
    assert.equal(toolMod.name, "ocsr-tools");
    assert.deepEqual(toolMod.inject, ["tools"]);
    assert.ok(toolMod.Config !== undefined, "tool entry must expose its Config schema");
    assert.equal(typeof gate.loadDriverBaseModels, "function");
  });

  test("package manifest satisfies the host compatibility gate", () => {
    // The install/load gate is the one failure mode that makes every other
    // adapter fix moot, so pin it against the host's own evaluator.
    const manifest = JSON.parse(readFileSync(join(REPO_ROOT, "package.json"), "utf8"));
    assert.equal(appBoot.evaluatePluginCompatibility(manifest, {}), undefined);
    const stale = { ...manifest, engines: { dsh: "^0.1.1-rc.2" } };
    assert.ok(appBoot.evaluatePluginCompatibility(stale, {}) !== undefined);
  });

  test("skill provider registers with the real host skills registry", async () => {
    const { SkillRegistry } = await importHost("@deepseek-ai/dsh-skill/lib/index.js");
    const ctx = new Context();
    await ctx.plugin(SkillRegistry);
    await ctx.plugin(skillMod);

    const summaries = await ctx.skills.list({});
    const summary = summaries.find((candidate) => candidate.name === "ocsr");
    assert.ok(summary, "ocsr must be listed by the real registry");
    assert.equal(summary.provider, "ocsr");
    assert.equal(summary.source, "bundled");
    assert.equal(summary.invocation.modelInvocable, true);
    assert.equal(summary.resourceBase.kind, "directory");

    const definition = await ctx.skills.get("ocsr", {});
    assert.ok(definition, "ocsr body must load by name");
    assert.match(definition.content, /OCSR/);
  });

  test("bundle patch entries point at real package exports", async () => {
    const yaml = await import(pathToFileURL(join(sandbox.host.packagesRoot, "yaml", "dist", "index.js")).href);
    const patch = yaml.parse(readFileSync(join(REPO_ROOT, "cordis.patch.yml"), "utf8"));
    const manifest = JSON.parse(readFileSync(join(REPO_ROOT, "package.json"), "utf8"));
    const entries = patch.flatMap((operation) => operation.insert ?? []);
    assert.deepEqual(entries.map((entry) => entry.id), ["ocsr-skill", "ocsr-tools"]);
    for (const entry of entries) {
      const subpath = entry.name === manifest.name ? "." : `.${entry.name.slice(manifest.name.length)}`;
      const target = manifest.exports[subpath];
      assert.ok(target, `patch entry ${entry.name} must map to an export`);
      assert.ok(existsSync(join(REPO_ROOT, target)), `export target ${target} must exist`);
    }
  });

  test("ocsr_dispatch compiles with the host schema DSL", () => {
    const definition = registerTool({ allowedModels: ["vendor/model"] });
    assert.equal(definition.name, "ocsr_dispatch");

    const schema = tools.projectToolSchema(definition, true);
    assert.deepEqual(schema.parameters.required, ["worker", "output_dir"]);
    assert.equal(schema.parameters.properties.background.default, false);
    assert.deepEqual(schema.parameters.properties.timeout_policy.enum, [
      "auto",
      "hierarchical_report",
      "leaf_kill",
    ]);

    const rendered = definition.output.render(
      {},
      { ok: true, kind: "foreground", jobId: null, code: 0 },
    );
    assert.equal(rendered.length, 1);
    assert.equal(rendered[0].type, "text");
    assert.match(rendered[0].text, /\[ok: true\]/);

    // Every shape the tool can return must satisfy the host's strict output schema.
    const outputSchema = definition.output.schema;
    const accepted = [
      { ok: true, kind: "foreground", jobId: null, command: "x", code: 0, stdout: "s", stderr: "" },
      { ok: true, kind: "background", jobId: "ocsr-dispatch-abc" },
      { ok: false, kind: "background", jobId: null, code: 5, stderr: "boom" },
      { ok: false, kind: "foreground", jobId: null, code: 4, stderr: "denied" },
    ];
    for (const value of accepted) {
      assert.deepEqual(tools.validateJsonSchemaValue(outputSchema, value), []);
    }
  });

  test("Config.allowedModels narrows the driver allowlist and never widens it", async () => {
    writeFileSync(allowlistFile, JSON.stringify(["vendor/model", "vendor/other"]));

    const allowed = registerTool({ allowedModels: ["vendor/model"] });
    const ok = await allowed.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR },
      execFor(),
    );
    assert.equal(ok.ok, true);
    assert.equal(ok.code, 0);

    const narrowed = registerTool({ allowedModels: ["vendor/other"] });
    const denied = await narrowed.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR },
      execFor(),
    );
    assert.equal(denied.ok, false);
    assert.equal(denied.code, 4);

    // The base allowlist is authoritative: a configured model outside it stays rejected.
    writeFileSync(allowlistFile, JSON.stringify(["vendor/other"]));
    const widened = registerTool({ allowedModels: ["vendor/model"] });
    const stillDenied = await widened.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR },
      execFor(),
    );
    assert.equal(stillDenied.code, 4);

    // An unconfigured base file fails closed even without a Config override.
    writeFileSync(allowlistFile, "[]");
    const unconfigured = registerTool(undefined);
    const closed = await unconfigured.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR },
      execFor(),
    );
    assert.equal(closed.code, 4);

    writeFileSync(allowlistFile, JSON.stringify(["vendor/model"]));
  });

  test("omitted Config leaves the driver allowlist in charge; explicit empty closes", async () => {
    writeFileSync(allowlistFile, JSON.stringify(["vendor/model"]));

    // Host resolution must keep "omitted" distinct from "explicitly empty".
    assert.equal(resolveConfig(undefined).allowedModels, undefined);
    assert.equal(resolveConfig({}).allowedModels, undefined);
    assert.deepEqual(resolveConfig({ allowedModels: [] }).allowedModels, []);

    const omitted = registerTool(undefined);
    const allowed = await omitted.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR },
      execFor(),
    );
    assert.equal(allowed.code, 0);

    const closed = registerTool({ allowedModels: [] });
    const denied = await closed.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR },
      execFor(),
    );
    assert.equal(denied.code, 4);
  });

  test("unparsable worker strings are rejected before dispatch", async () => {
    const definition = registerTool({ allowedModels: ["vendor/model"] });
    const result = await definition.execute(
      { worker: "no-separators", output_dir: OUT_DIR },
      execFor(),
    );
    assert.equal(result.ok, false);
    assert.equal(result.code, 4);
    assert.equal(result.kind, "foreground");
  });

  test("background dispatch feeds the host job output ring and maps outcomes", async () => {
    const { ctx, agent } = await createJobsContext();

    const definition = registerTool({}, { jobs: ctx.jobs });
    const abortController = new AbortController();
    const exec = { signal: abortController.signal, agent };

    const started = await definition.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR, background: true },
      exec,
    );
    assert.equal(started.ok, true);
    assert.equal(started.kind, "background");
    assert.equal(typeof started.jobId, "string");

    // The job belongs to the session, not to the tool call: cancelling the turn
    // after the job was registered must not kill the dispatch.
    abortController.abort();

    const completed = await waitForJob(ctx.jobs, started.jobId, agent.id);
    assert.equal(completed.status, "completed");

    const read = ctx.jobs.read(started.jobId, agent.id);
    const text = read.chunks
      .map((chunk) => `${chunk.channel ?? "stdout"}:${chunk.text}`)
      .join("");
    assert.match(text, /FAKE-STDOUT/);
    assert.match(text, /stderr:FAKE-STDERR/);

    // The job is owned by the session id, so that caller can list and kill it.
    assert.ok(ctx.jobs.list(agent.id).some((job) => job.id === started.jobId));

    process.env.FAKE_EXIT = "2";
    process.env.FAKE_MULTILINE = "1";
    try {
      const failed = await definition.execute(
        { worker: "p.md|vendor/model|L", output_dir: OUT_DIR, background: true },
        execFor(agent),
      );
      const failedView = await waitForJob(ctx.jobs, failed.jobId, agent.id);
      assert.equal(failedView.status, "failed");
      // `detail` is rendered as one status line: first non-empty stderr line only.
      assert.equal(failedView.detail, "FAKE-STDERR");
      assert.ok(!String(failedView.detail).includes("\n"));
    } finally {
      delete process.env.FAKE_EXIT;
      delete process.env.FAKE_MULTILINE;
    }
  });

  test("job_kill settles the dispatch as killed and forwards the reason", async () => {
    const { ctx, agent } = await createJobsContext();
    const definition = registerTool({}, { jobs: ctx.jobs });
    process.env.FAKE_SLEEP_MS = "1500";
    try {
      const started = await definition.execute(
        { worker: "p.md|vendor/model|L", output_dir: OUT_DIR, background: true },
        execFor(agent),
      );
      assert.equal(started.ok, true);
      assert.equal(ctx.jobs.kill(started.jobId, agent.id, "adapter-test-kill"), "requested");
      const view = await waitForJob(ctx.jobs, started.jobId, agent.id);
      assert.equal(view.status, "killed");
      assert.match(String(view.detail), /adapter-test-kill/);
    } finally {
      delete process.env.FAKE_SLEEP_MS;
    }
  });

  test("background dispatch reports code 5 when the jobs service is absent", async () => {
    const definition = registerTool({}, {});
    const result = await definition.execute(
      { worker: "p.md|vendor/model|L", output_dir: OUT_DIR, background: true },
      execFor(),
    );
    assert.equal(result.ok, false);
    assert.equal(result.kind, "background");
    assert.equal(result.code, 5);
  });
}

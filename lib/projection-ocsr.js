/**
 * OCSR dsh adapter — Phase 3 session projection (in-memory-only / best-effort).
 *
 * A read-only, observation-only projection unit that watches `ctx.jobs`
 * lifecycle changes and folds the visible OCSR job set into a session
 * projection under the `ocsr.jobs` key. It never copies OCSR dispatch
 * semantics into JS and never appends synthetic events to the session log;
 * synthetic `ocsr/jobs:update` events are fed only through the projection
 * registry's public `drive(session, event)` seam.
 *
 * Registration requires `sessionProjections`, `jobs`, and `sessions` to all
 * be available; if any is missing, or if a persistent projection cache is
 * mounted (which would capture phantom observedSeq watermarks), the plugin
 * skips registration with a warning and degrades to no-projection.
 *
 * @module @ocsr/dsh-ocsr/projection
 */
import { z } from "@deepseek-ai/schemastery";

/** Cordis plugin name (the id matched in cordis.patch.yml). */
const name = "ocsr-projection";
/** All services are optional and retrieved at runtime via `ctx.get(...)`. */
const inject = [];

/** Internal job snapshot (owner is internal-only; never exposed via wire.view). */
const jobSnapshotSchema = z.object({
  id: z.string(),
  kind: z.string(),
  status: z.string(),
  label: z.string(),
  owner: z.string().nullable().optional(),
  detail: z.string().optional(),
  startedAt: z.union([z.number(), z.string()]).optional(),
  finishedAt: z.union([z.number(), z.string()]).optional(),
});

/** Projection state: byId map + insertion order. */
const stateSchema = z.object({
  byId: z.record(z.string(), jobSnapshotSchema),
  order: z.array(z.string()),
});

/** Client-visible view: a subset that excludes the internal owner field. */
const viewSchema = z.object({
  jobs: z.array(
    z.object({
      id: z.string(),
      kind: z.string(),
      label: z.string(),
      status: z.string(),
      detail: z.string().optional(),
      startedAt: z.union([z.number(), z.string()]).optional(),
      finishedAt: z.union([z.number(), z.string()]).optional(),
    }),
  ),
});

/** Return the initial projection state. */
function init() {
  return { byId: {}, order: [] };
}

/**
 * Pure fold: only `ocsr/jobs:update` synthetic events are folded, everything
 * else returns the same state reference (Same-reference gate). This function
 * has no ctx/jobs access and must never read the job registry itself.
 */
function fold(state, event) {
  if (!event || event.type !== "ocsr/jobs:update") return state;
  const jobs = event.payload?.jobs;
  if (!Array.isArray(jobs)) return state;
  const byId = {};
  const order = [];
  for (const job of jobs) {
    if (!job || typeof job.id !== "string") continue;
    const snapshot = {
      id: job.id,
      kind: String(job.kind ?? ""),
      status: String(job.status ?? ""),
      label: String(job.label ?? ""),
      owner: job.owner ?? null,
    };
    if (job.detail !== undefined) snapshot.detail = job.detail;
    if (job.startedAt !== undefined) snapshot.startedAt = job.startedAt;
    if (job.finishedAt !== undefined) snapshot.finishedAt = job.finishedAt;
    byId[job.id] = snapshot;
    order.push(job.id);
  }
  return { byId, order };
}

/** Project a job snapshot to the client-visible subset (drops owner). */
function toViewJob(job) {
  if (!job) return null;
  const view = {
    id: job.id,
    kind: job.kind,
    label: job.label,
    status: job.status,
  };
  if (job.detail !== undefined) view.detail = job.detail;
  if (job.startedAt !== undefined) view.startedAt = job.startedAt;
  if (job.finishedAt !== undefined) view.finishedAt = job.finishedAt;
  return view;
}

/** Synchronous, pure wire.view mapping state -> client view. */
function view(state) {
  return {
    jobs: (state.order ?? [])
      .map((id) => toViewJob(state.byId?.[id]))
      .filter(Boolean),
  };
}

/** Normalize a raw jobs.list(job) snapshot into the internal jobSnapshot shape. */
function toJobSnapshot(job) {
  const snapshot = {
    id: String(job.id),
    kind: String(job.kind ?? ""),
    status: String(job.status ?? ""),
    label: String(job.label ?? ""),
    owner: job.ownerSession ?? job.owner ?? null,
  };
  if (job.detail !== undefined) snapshot.detail = job.detail;
  if (job.startedAt !== undefined) snapshot.startedAt = job.startedAt;
  if (job.finishedAt !== undefined) snapshot.finishedAt = job.finishedAt;
  return snapshot;
}

/**
 * Register the `ocsr.jobs` projection unit and wire the jobs lifecycle into
 * synthetic whole-value `ocsr/jobs:update` events. Fails closed: if the
 * registry/jobs/sessions trio is unavailable, or a projection cache is mounted,
 * registration is skipped with a warning.
 */
function apply(ctx) {
  const registry = ctx.get("sessionProjections");
  const jobs = ctx.get("jobs");
  const sessions = ctx.get("sessions");
  const cache = ctx.get("sessionProjectionCache");

  if (!registry || !jobs || !sessions) {
    ctx.logger?.warn?.(
      "ocsr-projection: skipped (missing sessionProjections/jobs/sessions)",
    );
    return;
  }
  if (cache) {
    ctx.logger?.warn?.(
      "ocsr-projection: skipped (projection cache mounted; in-memory projection would not persist)",
    );
    return;
  }

  // Per-session monotonic synthetic seq counter (always > 0).
  const lastDrivenSeq = new Map();

  function nextSeq(session, owner) {
    const key = String(session?.id ?? owner?.id ?? "anon");
    let base = 1;
    const events = session?.events;
    if (Array.isArray(events) && events.length > 0) {
      const last = events[events.length - 1];
      const lastSeq = Number(last?.seq ?? events.length - 1);
      base = Number.isFinite(lastSeq) && lastSeq >= 0 ? lastSeq + 1 : events.length;
      if (base < 1) base = 1;
    }
    const prev = lastDrivenSeq.get(key) ?? 0;
    const seq = Math.max(base, prev + 1, 1);
    lastDrivenSeq.set(key, seq);
    return seq;
  }

  let definition;
  try {
    definition = {
      key: "ocsr.jobs",
      stateSchema,
      init,
      apply: fold,
      wire: {
        viewSchema,
        view,
      },
      stateVersion: 1,
    };
    registry.register(definition);
  } catch (error) {
    ctx.logger?.warn?.(
      `ocsr-projection: register failed: ${String(error?.message ?? error)}`,
    );
    return;
  }

  jobs.onJobsChanged((owner) => {
    try {
      if (!owner || typeof owner !== "object") return;
      const session = sessions.get(owner.id);
      if (!session) return;
      const visible = jobs.list(owner) ?? [];
      const event = {
        type: "ocsr/jobs:update",
        seq: nextSeq(session, owner),
        timestamp: new Date().toISOString(),
        payload: { jobs: visible.map(toJobSnapshot) },
      };
      const result = registry.drive(session, event);
      if (result && typeof result.then === "function") {
        result.catch((error) => {
          ctx.logger?.warn?.(
            `ocsr-projection: job change drive failed: ${String(error?.message ?? error)}`,
          );
        });
      }
    } catch (error) {
      ctx.logger?.warn?.(
        `ocsr-projection: job change drive failed: ${String(error?.message ?? error)}`,
      );
    }
  });
}

export { apply, inject, name };

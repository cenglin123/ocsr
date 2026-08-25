/**
 * OCSR dsh adapter — Phase 1 skill provider.
 *
 * Registers the repo-root SKILL.md as an `ocsr` skill on the host-plane
 * `ctx.skills` registry. This is a thin mapping/wrapping layer only: it reads
 * the same package-root SKILL.md on every list()/get() call and never copies
 * OCSR orchestration semantics or skill body text into JS.
 *
 * @module @ocsr/dsh-ocsr
 */
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { BUNDLED_SKILL_RANK } from "@deepseek-ai/dsh-skill";
import { parse } from "yaml";

/** Provider object name (distinct from the Cordis plugin name `ocsr-skill`). */
const PROVIDER_NAME = "ocsr";

const SKILL_URL = new URL("../SKILL.md", import.meta.url);
const SKILL_PATH = fileURLToPath(SKILL_URL);
const PACKAGE_ROOT = fileURLToPath(new URL("../", import.meta.url));
const RESOURCE_BASE = { kind: "directory", path: PACKAGE_ROOT };

/** Cordis plugin name (the id matched in cordis.patch.yml). */
const name = "ocsr-skill";
/** Service required by the runtime provider. */
const inject = ["skills"];

function frontmatterBoolean(data, key) {
  if (!Object.hasOwn(data, key)) return undefined;
  const value = data[key];
  if (typeof value === "boolean") return value;
  if (value === 1 || value === "1") return true;
  if (value === 0 || value === "0") return false;
  if (typeof value === "string") {
    switch (value.toLowerCase()) {
      case "true":
      case "yes":
      case "on":
        return true;
      case "false":
      case "no":
      case "off":
        return false;
    }
  }
  throw new TypeError(`frontmatter field "${key}" must be a boolean`);
}

function parseInvocation(data) {
  const disableModelInvocation = frontmatterBoolean(data, "disable-model-invocation");
  const userInvocable = frontmatterBoolean(data, "user-invocable");
  return {
    modelInvocable: disableModelInvocation !== true,
    userInvocable: userInvocable !== false,
  };
}

function optionalMetadata(data) {
  const value = data.metadata;
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return { metadata: value };
  }
  return {};
}

function parseFrontmatter(raw) {
  const firstLineEnd = raw.indexOf("\n");
  if (firstLineEnd < 0) throw new Error("SKILL.md has no YAML frontmatter");
  if (raw.slice(0, firstLineEnd).replace(/\r$/, "") !== "---") {
    throw new Error("SKILL.md does not start with YAML frontmatter");
  }
  const start = firstLineEnd + 1;
  let lineStart = start;
  while (lineStart <= raw.length) {
    const nextNewline = raw.indexOf("\n", lineStart);
    const lineEnd = nextNewline < 0 ? raw.length : nextNewline;
    const line = raw.slice(lineStart, lineEnd).replace(/\r$/, "");
    if (line === "---") {
      const bodyStart = nextNewline < 0 ? raw.length : nextNewline + 1;
      const data = parse(raw.slice(start, lineStart));
      if (typeof data !== "object" || data === null || Array.isArray(data)) {
        throw new Error("SKILL.md frontmatter is not a mapping");
      }
      return { data, body: raw.slice(bodyStart) };
    }
    if (nextNewline < 0) break;
    lineStart = nextNewline + 1;
  }
  throw new Error("SKILL.md frontmatter not closed");
}

function signatureOf(raw) {
  return createHash("sha1").update(raw).digest("hex");
}

function buildSummary(data) {
  return {
    name: data.name,
    description: data.description,
    ...(typeof data.whenToUse === "string" && data.whenToUse.length > 0
      ? { whenToUse: data.whenToUse }
      : {}),
    invocation: parseInvocation(data),
    provider: PROVIDER_NAME,
    source: "bundled",
    resourceBase: RESOURCE_BASE,
    ...optionalMetadata(data),
  };
}

function readSkill(options) {
  return readFile(SKILL_URL, { encoding: "utf8", signal: options?.signal });
}

/** Register the bundled `ocsr` skill provider on `ctx.skills`. */
function apply(ctx) {
  ctx.skills.registerProvider((control) => {
    let lastSignature;
    const provider = {
      name: PROVIDER_NAME,
      async list(options) {
        let raw;
        try {
          raw = await readSkill(options);
        } catch (error) {
          ctx.logger?.warn(`ocsr provider: cannot read SKILL.md: ${String(error)}`);
          return [];
        }
        const signature = signatureOf(raw);
        if (lastSignature !== undefined && signature !== lastSignature) control.invalidate();
        lastSignature = signature;
        let parsed;
        try {
          parsed = parseFrontmatter(raw);
        } catch (error) {
          ctx.logger?.warn(`ocsr provider: ${String(error)}`);
          return [];
        }
        return [
          {
            ...buildSummary(parsed.data),
            rank: BUNDLED_SKILL_RANK,
            locator: SKILL_PATH,
            path: SKILL_PATH,
          },
        ];
      },
      async get(_candidate, options) {
        let raw;
        try {
          raw = await readSkill(options);
        } catch (error) {
          return undefined;
        }
        const signature = signatureOf(raw);
        if (lastSignature !== undefined && signature !== lastSignature) control.invalidate();
        lastSignature = signature;
        let parsed;
        try {
          parsed = parseFrontmatter(raw);
        } catch (error) {
          return undefined;
        }
        return {
          ...buildSummary(parsed.data),
          content: parsed.body.trim(),
          path: SKILL_PATH,
        };
      },
    };
    return provider;
  });
}

export { apply, inject, name };

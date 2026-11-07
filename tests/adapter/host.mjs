/**
 * Test support: locate a local DeepSeek Harness (dsh) >= 0.2 runtime and build
 * a throwaway sandbox in which the adapter's own ES modules resolve host
 * packages exactly like an installed profile would.
 *
 * The adapter imports `@deepseek-ai/dsh-skill`, `@deepseek-ai/dsh-tools`, and
 * `@deepseek-ai/schemastery` by bare specifier, so the regression tests must run
 * the real modules resolved from a real runtime; a fake would stop catching
 * exactly the host-contract drift these tests exist for.
 *
 * Resolution order for the runtime's dependency root:
 *   1. `DSH_RUNTIME_ROOT` (a dsh package dir, or a `node_modules` dir)
 *   2. the npm global `@deepseek-ai/dsh/node_modules` (where the CLI lives)
 *   3. `~/.dsh/profiles/node_modules` (the desktop-app profile resolver)
 * A candidate only counts when it holds `@deepseek-ai/dsh-skill` >= 0.2.
 */
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

/** Absolute repository root (this file lives in `tests/adapter/`). */
export const REPO_ROOT = resolve(HERE, "..", "..");

/** Path of the fake dispatcher copied into every sandbox. */
export const FAKE_DRIVER = join(HERE, "fixtures", "fake_driver.py");

function readVersion(packageDir) {
  try {
    return JSON.parse(readFileSync(join(packageDir, "package.json"), "utf8")).version ?? null;
  } catch {
    return null;
  }
}

/**
 * Compare against the adapter's declared floor (`^0.2.2-rc.1`): 0.2.0/0.2.1 are
 * rejected by the host's own compatibility gate, so accepting them here would
 * let the suite pass against a runtime the package cannot actually load on.
 */
function satisfiesHostLine(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version ?? "");
  if (match === null) return false;
  const major = Number(match[1]);
  const minor = Number(match[2]);
  const patch = Number(match[3]);
  if (major > 0) return true;
  if (major !== 0) return false;
  return minor > 2 || (minor === 2 && patch >= 2);
}

/** Normalize a candidate into the directory that owns `@deepseek-ai/*`. */
function packagesRootOf(candidate) {
  if (existsSync(join(candidate, "@deepseek-ai", "dsh-skill", "package.json"))) return candidate;
  const nested = join(candidate, "node_modules");
  if (existsSync(join(nested, "@deepseek-ai", "dsh-skill", "package.json"))) return nested;
  return null;
}

function candidateRoots() {
  const roots = [];
  if (process.env.APPDATA) {
    roots.push(join(process.env.APPDATA, "npm", "node_modules", "@deepseek-ai", "dsh", "node_modules"));
  }
  roots.push(join(homedir(), ".dsh", "profiles", "node_modules"));
  roots.push("/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules");
  roots.push("/opt/homebrew/lib/node_modules/@deepseek-ai/dsh/node_modules");
  roots.push("/usr/lib/node_modules/@deepseek-ai/dsh/node_modules");
  return roots;
}

/**
 * Find a usable dsh runtime.
 *
 * An explicitly set `DSH_RUNTIME_ROOT` pins the runtime and disables
 * auto-detection: a wrong value means "no runtime" (the suite skips) instead of
 * silently testing against a different installation than the caller pinned.
 * @returns {{ packagesRoot: string, version: string } | null} host facts, or null when absent.
 */
export function resolveHost() {
  const explicit = process.env.DSH_RUNTIME_ROOT;
  if (explicit) {
    const packagesRoot = packagesRootOf(explicit);
    if (packagesRoot === null) return null;
    const version = readVersion(join(packagesRoot, "@deepseek-ai", "dsh-skill"));
    if (!satisfiesHostLine(version)) return null;
    if (!existsSync(join(packagesRoot, "yaml", "package.json"))) return null;
    return { packagesRoot, version };
  }
  for (const candidate of candidateRoots()) {
    const packagesRoot = packagesRootOf(candidate);
    if (packagesRoot === null) continue;
    const version = readVersion(join(packagesRoot, "@deepseek-ai", "dsh-skill"));
    if (!satisfiesHostLine(version)) continue;
    if (!existsSync(join(packagesRoot, "yaml", "package.json"))) continue;
    return { packagesRoot, version };
  }
  return null;
}

/** Link one package directory; junction on Windows, symlink elsewhere. */
function linkPackage(source, target) {
  try {
    symlinkSync(source, target, "junction");
  } catch {
    cpSync(source, target, { recursive: true });
  }
}

/** Mirror a flat dependency root into `target`, preserving `@scope` grouping. */
function mirrorPackages(hostRoot, target) {
  mkdirSync(target, { recursive: true });
  for (const entry of readdirSync(hostRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const source = join(hostRoot, entry.name);
    if (entry.name.startsWith("@")) {
      const scopeTarget = join(target, entry.name);
      mkdirSync(scopeTarget, { recursive: true });
      for (const child of readdirSync(source, { withFileTypes: true })) {
        if (!child.isDirectory()) continue;
        linkPackage(join(source, child.name), join(scopeTarget, child.name));
      }
    } else {
      linkPackage(source, join(target, entry.name));
    }
  }
}

/**
 * Create a disposable sandbox containing an unmodified copy of `lib/`, the fake
 * dispatcher, and linked host packages.
 * @returns {{ dir: string, host: { packagesRoot: string, version: string }, cleanup: () => void } | null}
 */
export function makeSandbox() {
  const host = resolveHost();
  if (host === null) return null;
  const dir = mkdtempSync(join(tmpdir(), "ocsr-adapter-test-"));
  cpSync(join(REPO_ROOT, "lib"), join(dir, "lib"), { recursive: true });
  cpSync(join(REPO_ROOT, "SKILL.md"), join(dir, "SKILL.md"));
  mkdirSync(join(dir, "scripts"), { recursive: true });
  cpSync(FAKE_DRIVER, join(dir, "scripts", "ocsr_dispatch.py"));
  writeFileSync(join(dir, "package.json"), '{"type":"module"}\n');
  mirrorPackages(host.packagesRoot, join(dir, "node_modules"));
  return {
    dir,
    host,
    cleanup() {
      rmSync(dir, { recursive: true, force: true });
    },
  };
}

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

// Phase 5 acceptance: no page promises a feature it does not have ("coming soon", "not yet available" placeholders).
const ROOTS = ["app", "components", "lib"].map((d) => path.resolve(import.meta.dirname, "..", d));
const PLACEHOLDER = /coming soon|under construction|stay tuned/i;

function files(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) return files(p);
    return /\.(tsx?|css)$/.test(e.name) && !e.name.endsWith(".test.ts") ? [p] : [];
  });
}

test("no page says a feature is coming soon", () => {
  const hits = ROOTS.flatMap(files).filter((f) => PLACEHOLDER.test(fs.readFileSync(f, "utf8")));
  assert.deepEqual(hits, []);
});

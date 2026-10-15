import assert from "node:assert/strict";
import { test } from "node:test";
import { THEME_CHOICES, THEME_INIT_SCRIPT, THEME_KEY, parseTheme, stepTheme } from "./theme.ts";

test("parseTheme accepts light and dark and treats everything else as system", () => {
  assert.equal(parseTheme("light"), "light");
  assert.equal(parseTheme("dark"), "dark");
  for (const v of ["system", "", "LIGHT", "blue", null, undefined]) assert.equal(parseTheme(v), "system");
});

test("stepTheme moves through System, Light, Dark and wraps", () => {
  assert.deepEqual(THEME_CHOICES.map((c) => c.value), ["system", "light", "dark"]);
  assert.equal(stepTheme("system", 1), "light");
  assert.equal(stepTheme("dark", 1), "system");
  assert.equal(stepTheme("system", -1), "dark");
  assert.equal(stepTheme("light", -1), "system");
});

// The init script is the only thing between a stored choice and the first paint, so run it
// against a stub document and check what it does with each stored value.
function run(stored: string | null | "throw") {
  const attrs: Record<string, string> = {};
  const localStorage = {
    getItem(key: string) {
      if (stored === "throw") throw new Error("blocked");
      assert.equal(key, THEME_KEY);
      return stored;
    },
  };
  const document = { documentElement: { setAttribute: (k: string, v: string) => { attrs[k] = v; } } };
  new Function("localStorage", "document", THEME_INIT_SCRIPT)(localStorage, document);
  return attrs;
}

test("the init script applies a stored light or dark choice and nothing else", () => {
  assert.deepEqual(run("light"), { "data-theme": "light" });
  assert.deepEqual(run("dark"), { "data-theme": "dark" });
  assert.deepEqual(run(null), {});
  assert.deepEqual(run("neon"), {});
});

test("the init script survives blocked storage", () => {
  assert.deepEqual(run("throw"), {});
});

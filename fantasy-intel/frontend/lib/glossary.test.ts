import assert from "node:assert/strict";
import { test } from "node:test";
import { GLOSSARY } from "./glossary.ts";

test("every glossary term has a plain sentence definition", () => {
  for (const [term, text] of Object.entries(GLOSSARY)) {
    assert.ok(text.length > 20, term);
    assert.ok(text.endsWith("."), `${term} should end with a full stop`);
    assert.ok(!/!/.test(text), `${term}: no exclamation marks`);
  }
});

test("the dash is defined as missing, never zero", () => {
  assert.match(GLOSSARY["—"], /never zero/);
});

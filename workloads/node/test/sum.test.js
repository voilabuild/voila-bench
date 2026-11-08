import { test } from "node:test";
import assert from "node:assert/strict";
import { sumColumn } from "../sum.js";

test("sums a column", () => {
  assert.equal(sumColumn("a,b\n1,2\n3,4\n", "b"), 6);
});

test("rejects a missing column", () => {
  assert.throws(() => sumColumn("a\n1\n", "z"), /no column z/);
});

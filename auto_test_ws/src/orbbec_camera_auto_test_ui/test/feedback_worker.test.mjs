import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../feedback_worker/worker.mjs";

const origin = "https://ob-auto-test-feedback.example";
const env = {
  GITHUB_ISSUES_TOKEN: "mock-token",
  TURNSTILE_SECRET: "mock-secret",
  TURNSTILE_SITE_KEY: "mock-public-key",
};
const payload = { suggestion: "希望导出设置\r<script>", token: "challenge" };

function post(data, requestOrigin = origin) {
  return new Request(origin + "/api/feedback", {
    method: "POST",
    headers: { "Origin": requestOrigin, "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

test("publishes only to the configured GitHub repository after verification", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.startsWith("https://challenges.cloudflare.com/")) {
      assert.equal(options.body.get("response"), "challenge");
      assert.equal(options.body.get("secret"), env.TURNSTILE_SECRET);
      return Response.json({ success: true, hostname: new URL(origin).hostname, action: "feedback" });
    }
    assert.equal(url, "https://api.github.com/repos/obshenlanzhou/ob_Auto_Test_ROS/issues");
    assert.equal(options.redirect, "manual");
    assert.equal(options.headers.Authorization, "Bearer mock-token");
    const { title, body } = JSON.parse(options.body);
    assert.equal(title, "工具优化");
    assert.match(body, /### 改进建议\n\n    希望导出设置\n    <script>/);
    assert.doesNotMatch(body, /ROS jazzy/);
    return Response.json({ number: 42, html_url: "https://ignored.example" }, { status: 201 });
  });
  const response = await worker.fetch(post({ ...payload, context: "ROS jazzy" }), env);
  assert.equal(response.status, 201);
  assert.deepEqual(await response.json(), {
    number: 42,
    url: "https://github.com/obshenlanzhou/ob_Auto_Test_ROS/issues/42",
  });
  assert.equal(fetch.mock.callCount(), 2);
});

test("rejects malformed requests and wrong or reused challenges before publishing", async (t) => {
  const challenges = [
    { success: true, hostname: "another.example", action: "feedback" },
    { success: true, hostname: new URL(origin).hostname, action: "another-form" },
    { success: false, "error-codes": ["timeout-or-duplicate"] },
  ];
  const fetch = t.mock.method(globalThis, "fetch", async (url) => {
    assert.match(url, /^https:\/\/challenges\.cloudflare\.com\//);
    return Response.json(challenges.shift());
  });
  assert.equal((await worker.fetch(post(payload, "https://other.example"), env)).status, 403);
  assert.equal((await worker.fetch(post({ ...payload, token: "" }), env)).status, 400);
  assert.equal((await worker.fetch(post({ ...payload, suggestion: "a".repeat(16001) }), env)).status, 413);
  assert.equal((await worker.fetch(post({ actual: "旧版测试反馈", token: "challenge" }), env)).status, 400);
  assert.equal((await worker.fetch(post(payload), {})).status, 503);
  assert.equal(fetch.mock.callCount(), 0);
  for (let index = 0; index < 3; index++) {
    assert.equal((await worker.fetch(post(payload), env)).status, 400);
  }
  assert.equal(fetch.mock.callCount(), 3);
});

test("shows the default title and suggestion scope without exposing secrets", async () => {
  const response = await worker.fetch(new Request(origin + "/feedback"), env);
  const content = await response.text();
  assert.match(content, /data-sitekey="mock-public-key"/);
  assert.match(content, /name="title" value="工具优化"/);
  assert.match(content, /暂不支持反馈测试问题/);
  assert.match(content, /name="suggestion"/);
  assert.doesNotMatch(content, /复现步骤|诊断摘要|name="actual"/);
  assert.doesNotMatch(content, /mock-token|mock-secret/);
  assert.match(response.headers.get("Content-Security-Policy"), /script-src 'nonce-/);
});

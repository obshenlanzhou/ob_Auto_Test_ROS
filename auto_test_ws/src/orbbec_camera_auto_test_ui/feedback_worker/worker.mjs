const REPOSITORY = "obshenlanzhou/ob_Auto_Test_ROS";
const ISSUES = `https://github.com/${REPOSITORY}/issues`;
const TURNSTILE_ORIGIN = "https://challenges.cloudflare.com";
const DEFAULT_TITLE = "工具优化";
const MAX_BYTES = 16000;

class FeedbackError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function ready(env) {
  return Boolean(env.GITHUB_ISSUES_TOKEN && env.TURNSTILE_SECRET &&
    /^[a-zA-Z0-9_-]{4,100}$/.test(env.TURNSTILE_SITE_KEY || ""));
}

function json(payload, status = 200) {
  return Response.json(payload, { status, headers: { "Cache-Control": "no-store" } });
}

async function readFeedback(request) {
  const reader = request.body?.getReader();
  if (!reader) throw new FeedbackError(400, "请填写反馈内容。");
  const bytes = new Uint8Array(MAX_BYTES);
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    if (size + value.length > bytes.length) {
      await reader.cancel();
      throw new FeedbackError(413, "反馈内容过长，请精简后提交。");
    }
    bytes.set(value, size);
    size += value.length;
  }
  try {
    const data = JSON.parse(new TextDecoder().decode(bytes.subarray(0, size)));
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error();
    return data;
  } catch {
    throw new FeedbackError(400, "反馈格式无效。");
  }
}

function field(data, name, maxLength, required = false) {
  const value = typeof data[name] === "string" ? data[name].trim() : "";
  if (value.length > maxLength || (required && !value)) {
    throw new FeedbackError(400, "请检查必填内容和字数限制。");
  }
  return value;
}

async function submitFeedback(request, env, url) {
  if (request.headers.get("Origin") !== url.origin ||
    request.headers.get("Content-Type")?.split(";", 1)[0].trim() !== "application/json") {
    throw new FeedbackError(403, "请在工具改进建议窗口提交。");
  }
  if (!ready(env)) throw new FeedbackError(503, "反馈服务正在配置，请稍后再试。");

  const data = await readFeedback(request);
  const title = (field(data, "title", 100) || DEFAULT_TITLE).replace(/\s+/g, " ").replaceAll("@", "＠");
  const suggestion = field(data, "suggestion", 1500, true);
  const token = field(data, "token", 2048);
  if (!token) throw new FeedbackError(400, "请先完成人机验证。");

  const verification = await fetch(`${TURNSTILE_ORIGIN}/turnstile/v0/siteverify`, {
    method: "POST",
    body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token }),
    signal: AbortSignal.timeout(8000),
  });
  if (!verification.ok) throw new FeedbackError(502, "验证服务暂时不可用，请重试。");
  const challenge = await verification.json();
  if (!challenge.success || challenge.hostname !== url.hostname || challenge.action !== "feedback") {
    throw new FeedbackError(400, "验证无效或已过期，请重新验证。");
  }

  const body = suggestion.split(/\r\n|[\r\n]/).map((line) => `    ${line}`).join("\n");
  const result = await fetch(`https://api.github.com/repos/${REPOSITORY}/issues`, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_ISSUES_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "ob-auto-test-feedback",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      title,
      body: `来自 Orbbec 测试工具的改进建议。\n\n### 改进建议\n\n${body}`,
    }),
    redirect: "manual",
    signal: AbortSignal.timeout(8000),
  });
  if (result.status !== 201) {
    console.warn("GitHub issue creation failed", result.status);
    throw new FeedbackError(502, "暂时无法创建 GitHub Issue，请稍后重试。");
  }
  const issue = await result.json();
  if (!Number.isInteger(issue.number) || issue.number < 1) {
    throw new FeedbackError(502, "GitHub 返回了异常结果，请先查看 Issues 列表。");
  }
  return json({ number: issue.number, url: `${ISSUES}/${issue.number}` }, 201);
}

function feedbackPage(env) {
  const nonce = crypto.randomUUID();
  const configured = ready(env);
  return new Response(`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>工具改进建议</title>
  <style nonce="${nonce}">
    :root { color-scheme: light dark; font: 14px system-ui, sans-serif; }
    body { margin: 0; padding: 14px; background: Canvas; color: CanvasText; }
    form { max-width: 640px; margin: auto; display: grid; gap: 12px; }
    label { display: grid; gap: 5px; }
    input, textarea, button { font: inherit; padding: 8px; border: 1px solid #7c8795; border-radius: 6px; }
    textarea { resize: vertical; }
    button { background: #0d6578; color: white; justify-self: start; padding-inline: 14px; cursor: pointer; }
    button:disabled { opacity: .55; cursor: default; }
    p { margin: 0; }
    .suggestion-scope { font-weight: 600; }
  </style>
</head>
<body>
  <form id="feedback">
    <p class="suggestion-scope">此入口仅接收工具改进建议，暂不支持反馈测试问题。</p>
    <p id="status" role="status">${configured ? "建议会发布到公开的 GitHub Issues。" : "反馈服务正在配置，请稍后再试。"}</p>
    <label>标题<input name="title" value="${DEFAULT_TITLE}" maxlength="100"></label>
    <label>改进建议<textarea name="suggestion" rows="5" maxlength="1500" required></textarea></label>
    ${configured ? `<div class="cf-turnstile" data-sitekey="${env.TURNSTILE_SITE_KEY}" data-action="feedback"></div>` : ""}
    <button id="submit" ${configured ? "" : "disabled"}>提交建议</button>
    <a id="link" hidden target="_blank" rel="noopener noreferrer"></a>
  </form>
  ${configured ? `<script src="${TURNSTILE_ORIGIN}/turnstile/v0/api.js" async defer></script>` : ""}
  <script nonce="${nonce}">
    const form = document.getElementById("feedback");
    const status = document.getElementById("status");
    const button = document.getElementById("submit");
    const link = document.getElementById("link");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (button.disabled) return;
      const data = Object.fromEntries(new FormData(form));
      data.token = data["cf-turnstile-response"] || "";
      delete data["cf-turnstile-response"];
      if (!data.token) { status.textContent = "请先完成人机验证。"; return; }
      button.disabled = true;
      status.textContent = "正在提交建议…";
      try {
        const response = await fetch("/api/feedback", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error);
        status.textContent = "建议已提交。";
        link.textContent = "查看 Issue #" + result.number;
        link.href = result.url;
        link.hidden = false;
      } catch (error) {
        status.textContent = error.message || "建议提交失败，请重试。";
        button.disabled = false;
        window.turnstile?.reset();
      }
    });
  </script>
</body>
</html>`, {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "Referrer-Policy": "no-referrer",
      "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}' ${TURNSTILE_ORIGIN}; ` +
        `style-src 'nonce-${nonce}'; frame-src ${TURNSTILE_ORIGIN}; connect-src 'self'; base-uri 'none'; form-action 'none'`,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") return json({ ready: ready(env) });
    if (request.method === "GET" && ["/", "/feedback"].includes(url.pathname)) return feedbackPage(env);
    if (request.method !== "POST" || url.pathname !== "/api/feedback") return json({ error: "未找到此接口。" }, 404);
    try {
      return await submitFeedback(request, env, url);
    } catch (error) {
      if (error instanceof FeedbackError) return json({ error: error.message }, error.status);
      return json({ error: "反馈服务暂时不可用，请稍后重试。" }, 502);
    }
  },
};

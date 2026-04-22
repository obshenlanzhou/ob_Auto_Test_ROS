const state = {
  profiles: [],
  selectedProfile: null,
  logOffset: 0,
  polling: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    const message = payload.errors ? payload.errors.join("\n") : payload.error || response.statusText;
    throw new Error(message);
  }
  return payload;
}

function formPayload() {
  return {
    ros_setup: $("rosSetup").value.trim(),
    camera_setup: $("cameraSetup").value.trim(),
    mode: $("mode").value,
    profile: $("profile").value,
    performance_scenario: $("performanceScenario").value,
    duration: $("duration").value.trim(),
    camera_name: $("cameraName").value.trim(),
    serial_number: $("serialNumber").value.trim(),
    usb_port: $("usbPort").value.trim(),
    config_file_path: $("configFilePath").value.trim(),
    launch_file: $("launchFile").value.trim(),
    launch_args: $("launchArgs").value,
  };
}

function setStatus(status) {
  const node = $("runStatus");
  node.textContent = status || "idle";
  node.className = `status-pill ${status || "idle"}`;
  const running = ["starting", "running", "stopping"].includes(status);
  $("startButton").disabled = running;
  $("stopButton").disabled = !running;
}

function renderCommands(commands = []) {
  const box = $("commandBox");
  box.innerHTML = "";
  for (const command of commands) {
    const line = document.createElement("div");
    line.textContent = command;
    box.appendChild(line);
  }
}

function appendLogs(lines = []) {
  if (!lines.length) return;
  const log = $("logOutput");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent += `${lines.join("\n")}\n`;
  if (atBottom) {
    log.scrollTop = log.scrollHeight;
  }
}

function updateScenarioOptions() {
  const selected = state.profiles.find((profile) => profile.name === $("profile").value);
  state.selectedProfile = selected || null;
  $("performanceScenario").innerHTML = "";

  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "全部 / 默认";
  $("performanceScenario").appendChild(empty);

  for (const scenario of selected?.performance_scenarios || []) {
    const option = document.createElement("option");
    option.value = scenario.name;
    option.textContent = scenario.duration
      ? `${scenario.name} (${scenario.duration}s)`
      : scenario.name;
    $("performanceScenario").appendChild(option);
  }

  if (selected?.launch_file) {
    $("launchFile").placeholder = selected.launch_file;
  }
}

async function loadConfig() {
  const config = await api("/api/config");
  $("rosSetup").value = config.ros_setup || "";
  $("cameraSetup").value = config.camera_setup || "";
  $("workspacePath").textContent = `工作区: ${config.auto_test_ws}`;
}

async function loadProfiles() {
  const payload = await api("/api/profiles");
  state.profiles = payload.profiles || [];
  $("profileCount").textContent = String(state.profiles.length);
  $("profile").innerHTML = "";
  for (const profile of state.profiles) {
    const option = document.createElement("option");
    option.value = profile.name;
    option.textContent = profile.name;
    $("profile").appendChild(option);
  }
  updateScenarioOptions();
}

async function pollStatus() {
  try {
    const payload = await api(`/api/status?offset=${state.logOffset}`);
    setStatus(payload.status || "idle");
    if (payload.run_id) {
      $("runMeta").textContent = `${payload.run_id} ${payload.exit_code === null ? "" : `exit=${payload.exit_code}`}`;
      $("currentRunId").textContent = payload.run_id;
      $("currentMode").textContent = payload.mode || "-";
    } else {
      $("runMeta").textContent = "";
      $("currentRunId").textContent = "-";
      $("currentMode").textContent = "-";
    }
    if (payload.command_lines) {
      renderCommands(payload.command_lines);
    }
    appendLogs(payload.logs || []);
    state.logOffset = payload.log_offset || state.logOffset;
    if (["passed", "failed", "interrupted"].includes(payload.status)) {
      await loadRuns();
    }
  } catch (error) {
    appendLogs([`[UI] status poll failed: ${error.message}`]);
  }
}

async function startRun(event) {
  event.preventDefault();
  $("logOutput").textContent = "";
  $("reportView").textContent = "测试运行中...";
  state.logOffset = 0;
  try {
    const payload = await api("/api/run", {
      method: "POST",
      body: JSON.stringify(formPayload()),
    });
    setStatus(payload.status);
    renderCommands(payload.command_lines || []);
    appendLogs(payload.logs || []);
    state.logOffset = payload.log_offset || 0;
  } catch (error) {
    setStatus("failed");
    appendLogs([`[UI] start failed: ${error.message}`]);
  }
}

async function stopRun() {
  try {
    const payload = await api("/api/stop", { method: "POST", body: "{}" });
    setStatus(payload.status);
    appendLogs(payload.logs || []);
  } catch (error) {
    appendLogs([`[UI] stop failed: ${error.message}`]);
  }
}

function runItem(run) {
  const item = document.createElement("div");
  item.className = "run-item";

  const title = document.createElement("div");
  title.className = "run-title";
  title.textContent = run.run_id;

  const subtitle = document.createElement("div");
  subtitle.className = "run-subtitle";
  subtitle.textContent = `${run.mode || "unknown"} | ${run.started_at || ""} | ${run.results_dir}`;

  const badge = document.createElement("span");
  badge.className = `badge ${run.status}`;
  badge.textContent = run.status || "unknown";
  title.append(" ");
  title.appendChild(badge);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost";
  button.textContent = "查看";
  button.addEventListener("click", () => loadRunDetail(run.run_id));

  item.append(title, button, subtitle);
  return item;
}

async function loadRuns() {
  const payload = await api("/api/runs");
  const list = $("runsList");
  list.innerHTML = "";
  for (const run of payload.runs || []) {
    list.appendChild(runItem(run));
  }
  if (!list.children.length) {
    list.textContent = "暂无历史记录。";
  }
}

function renderJsonSummary(results = {}) {
  const blocks = [];
  for (const [name, result] of Object.entries(results)) {
    const lines = [
      `${name}: ${result.status || "unknown"}`,
      `profile: ${result.profile_name || ""}`,
      `launch: ${result.launch_file || ""}`,
    ];
    if (Array.isArray(result.scenarios)) {
      lines.push(`scenarios: ${result.scenarios.map((item) => `${item.name}:${item.status}`).join(", ")}`);
    }
    blocks.push({ title: `${name} result`, text: lines.join("\n") });
  }
  return blocks;
}

async function loadRunDetail(runId) {
  const payload = await api(`/api/runs/${encodeURIComponent(runId)}`);
  $("reportTitle").textContent = runId;
  const view = $("reportView");
  view.innerHTML = "";

  for (const block of renderJsonSummary(payload.results)) {
    const section = document.createElement("section");
    section.className = "summary-block";
    section.innerHTML = `<h3></h3><pre></pre>`;
    section.querySelector("h3").textContent = block.title;
    section.querySelector("pre").textContent = block.text;
    view.appendChild(section);
  }

  for (const [name, text] of Object.entries(payload.summaries || {})) {
    const section = document.createElement("section");
    section.className = "summary-block";
    section.innerHTML = `<h3></h3><pre></pre>`;
    section.querySelector("h3").textContent = `${name}.md`;
    section.querySelector("pre").textContent = text;
    view.appendChild(section);
  }

  if (!view.children.length) {
    view.textContent = "这条记录还没有可展示的报告。";
  }
}

async function init() {
  setStatus("idle");
  $("runForm").addEventListener("submit", startRun);
  $("stopButton").addEventListener("click", stopRun);
  $("refreshProfiles").addEventListener("click", loadProfiles);
  $("refreshRuns").addEventListener("click", loadRuns);
  $("profile").addEventListener("change", updateScenarioOptions);

  await loadConfig();
  await loadProfiles();
  await loadRuns();
  await pollStatus();
  state.polling = setInterval(pollStatus, 1000);
}

init().catch((error) => {
  setStatus("failed");
  appendLogs([`[UI] init failed: ${error.message}`]);
});

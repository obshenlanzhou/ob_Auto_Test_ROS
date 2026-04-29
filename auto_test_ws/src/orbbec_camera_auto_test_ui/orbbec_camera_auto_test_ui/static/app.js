const state = {
  profiles: {
    functional: [],
    performance: [],
  },
  config: {},
  selectedFunctionalProfile: null,
  selectedPerformanceProfile: null,
  logOffset: 0,
  polling: null,
};

const $ = (id) => document.getElementById(id);
const MAX_VISIBLE_LOG_LINES = 500;

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
    functional_profile: $("functionalProfile").value,
    performance_profile: $("performanceProfile").value,
    performance_scenario: $("performanceScenario").value,
    duration: $("duration").value.trim(),
    stable_seconds: $("stableSeconds").value.trim(),
    stream_timeout: $("streamTimeout").value.trim(),
    max_gap_seconds: $("maxGapSeconds").value.trim(),
    restart_delay: $("restartDelay").value.trim(),
    image_topics: $("imageTopics").value,
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
  const visibleLines = log.textContent.split("\n");
  if (visibleLines.length > MAX_VISIBLE_LOG_LINES + 1) {
    log.textContent = `${visibleLines.slice(-(MAX_VISIBLE_LOG_LINES + 1)).join("\n")}`;
  }
  if (atBottom) {
    log.scrollTop = log.scrollHeight;
  }
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = String(Math.floor(total / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const secs = String(total % 60).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
}

function formatNumber(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toFixed(digits);
}

function renderPerformance(performance = {}) {
  $("perfElapsed").textContent = formatDuration(performance.elapsed_seconds);
  $("perfCpu").textContent = performance.available
    ? `${formatNumber(performance.cpu_percent, 1)}%`
    : "--";
  $("perfRam").textContent = performance.available
    ? `${formatNumber(performance.memory_rss_mb, 1)} MB`
    : "--";
  $("perfPidCount").textContent = performance.available
    ? String(performance.pid_count || 0)
    : "--";

  const systemBody = $("systemTableBody");
  systemBody.innerHTML = "";
  const scopes = (performance.system_scopes || []).filter(
    (scope) => !(scope.scope === "total" && scope.camera_name === "all")
  );
  if (!scopes.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.textContent = performance.available ? "暂无资源明细。" : "等待资源采样。";
    row.appendChild(cell);
    systemBody.appendChild(row);
  } else {
    for (const scope of scopes) {
      const row = document.createElement("tr");
      const values = [
        scope.label || scope.camera_name || scope.scope || "",
        `${formatNumber(scope.cpu_percent, 2)}%`,
        `${formatNumber(scope.memory_rss_mb, 1)} MB`,
        String(scope.pid_count || 0),
      ];
      for (const value of values) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      }
      systemBody.appendChild(row);
    }
  }

  const body = $("fpsTableBody");
  body.innerHTML = "";
  const topics = performance.fps_topics || [];
  if (!topics.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.textContent = performance.available ? "暂无 FPS 采样。" : "等待性能压测数据。";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  for (const topic of topics) {
    const row = document.createElement("tr");
    const values = [
      topic.topic || topic.label || "",
      topic.resolution || "-",
      topic.stream_format || "-",
      formatNumber(topic.current_fps, 2),
      formatNumber(topic.avg_fps, 2),
      formatNumber(topic.ideal_fps, 2),
      String(topic.dropped_frames || 0),
      `${formatNumber((topic.drop_rate || 0) * 100, 3)}%`,
    ];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
}

function renderRestart(restart = {}, mode = "") {
  const visible = mode === "restart" || restart.available;
  $("restartMetrics").classList.toggle("is-hidden", !visible);
  $("restartMessage").classList.toggle("is-hidden", !visible || !restart.message);
  if (!visible) return;

  $("restartSuccessCount").textContent = restart.available
    ? String(restart.successful_restarts || 0)
    : "--";
  $("restartAttemptCount").textContent = restart.available
    ? String(restart.launch_attempts || 0)
    : "--";
  $("restartAttemptStatus").textContent = restart.available
    ? restart.current_attempt_status || "-"
    : "--";
  $("restartOverallStatus").textContent = restart.available
    ? restart.status || "-"
    : "--";
  $("restartMessage").textContent = restart.message || "";
}

function updateScenarioOptions() {
  const selected = state.profiles.performance.find(
    (profile) => profile.name === $("performanceProfile").value
  );
  state.selectedPerformanceProfile = selected || null;
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

function updateModeControls() {
  const mode = $("mode").value;
  const needsFunctional = mode === "functional" || mode === "all";
  const needsPerformance = mode === "performance" || mode === "all";
  const needsPerformanceRuntime = mode === "performance" || mode === "restart" || mode === "all";
  const needsRestart = mode === "restart";
  $("functionalProfileField").classList.toggle("is-hidden", !needsFunctional);
  $("performanceProfileField").classList.toggle("is-hidden", !needsPerformance);
  $("performanceScenario").closest("label").classList.toggle("is-hidden", !needsPerformance);
  $("duration").closest("label").classList.toggle("is-hidden", !needsPerformanceRuntime);
  $("restartFields").classList.toggle("is-hidden", !needsRestart);

  const functional = state.profiles.functional.find(
    (profile) => profile.name === $("functionalProfile").value
  );
  const performance = state.profiles.performance.find(
    (profile) => profile.name === $("performanceProfile").value
  );
  state.selectedFunctionalProfile = functional || null;
  state.selectedPerformanceProfile = performance || null;
  const activeProfile = needsRestart ? null : needsPerformance ? performance : functional;
  if (activeProfile?.launch_file) {
    $("launchFile").placeholder = activeProfile.launch_file;
  } else if (needsRestart) {
    $("launchFile").placeholder = "gemini_330_series.launch.py";
  }
}

function fillProfileSelect(selectId, profiles, preferredName) {
  const select = $(selectId);
  select.innerHTML = "";
  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.name;
    option.textContent = profile.name;
    select.appendChild(option);
  }
  if (preferredName && profiles.some((profile) => profile.name === preferredName)) {
    select.value = preferredName;
  }
}

async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;
  $("rosSetup").value = config.ros_setup || "";
  $("cameraSetup").value = config.camera_setup || "";
  $("mode").value = config.mode || "functional";
  $("duration").value = config.duration || "";
  $("stableSeconds").value = config.stable_seconds || "10";
  $("streamTimeout").value = config.stream_timeout || "60";
  $("maxGapSeconds").value = config.max_gap_seconds || "1.5";
  $("restartDelay").value = config.restart_delay || "2";
  $("imageTopics").value = config.image_topics || "";
  $("workspacePath").textContent = `工作区: ${config.auto_test_ws}`;
}

async function loadProfiles() {
  const payload = await api("/api/profiles");
  state.profiles = {
    functional: payload.profiles_by_type?.functional || [],
    performance: payload.profiles_by_type?.performance || [],
  };
  $("profileCount").textContent = `${state.profiles.functional.length}/${state.profiles.performance.length}`;
  fillProfileSelect(
    "functionalProfile",
    state.profiles.functional,
    state.config.functional_profile || "gemini_330_series"
  );
  fillProfileSelect(
    "performanceProfile",
    state.profiles.performance,
    state.config.performance_profile || "gemini_330_series"
  );
  updateScenarioOptions();
  if (state.config.performance_scenario) {
    $("performanceScenario").value = state.config.performance_scenario;
  }
  updateModeControls();
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
    renderPerformance(payload.performance || {});
    renderRestart(payload.restart || {}, payload.mode || $("mode").value);
    appendLogs(payload.logs || []);
    state.logOffset = payload.log_offset || state.logOffset;
    if (["passed", "failed", "interrupted", "warning"].includes(payload.status)) {
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
    renderRestart(payload.restart || {}, payload.mode || $("mode").value);
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

async function deleteRun(runId) {
  if (!window.confirm(`删除历史记录 ${runId}？`)) {
    return;
  }
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
    if ($("reportTitle").textContent === runId) {
      $("reportTitle").textContent = "";
      $("reportView").textContent = "选择一条历史记录查看结果。";
    }
    await loadRuns();
  } catch (error) {
    appendLogs([`[UI] delete failed: ${error.message}`]);
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

  const actions = document.createElement("div");
  actions.className = "run-actions";

  const viewButton = document.createElement("button");
  viewButton.type = "button";
  viewButton.className = "ghost";
  viewButton.textContent = "查看";
  viewButton.addEventListener("click", () => loadRunDetail(run.run_id));

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "danger";
  deleteButton.textContent = "删除";
  deleteButton.addEventListener("click", () => deleteRun(run.run_id));

  actions.append(viewButton, deleteButton);
  item.append(title, actions, subtitle);
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
  $("mode").addEventListener("change", updateModeControls);
  $("functionalProfile").addEventListener("change", updateModeControls);
  $("performanceProfile").addEventListener("change", () => {
    updateScenarioOptions();
    updateModeControls();
  });

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

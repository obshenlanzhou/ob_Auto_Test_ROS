const state = {
  config: {},
  logOffset: 0,
  polling: null,
  selectedRunId: null,
};

const $ = (id) => document.getElementById(id);
const MAX_VISIBLE_LOG_LINES = 500;
const DEFAULT_SETUPS = {
  "2": {
    ros: "/opt/ros/humble/setup.bash",
    camera: "",
    cameraPlaceholder: "/path/to/orbbecsdk_ros2/install/setup.bash",
  },
  "1": {
    ros: "/opt/ros/one/setup.bash",
    camera: "",
    cameraPlaceholder: "/path/to/orbbecsdk_ros1/devel/setup.bash",
  },
};
const SINGLE_CAMERA_LAUNCH_FILES = {
  "2": [
    "astra.launch.py",
    "astra2.launch.py",
    "dabai_a.launch.py",
    "dabai_al.launch.py",
    "dabai_dcw2.launch.py",
    "dabai_max_pro.launch.py",
    "femto.launch.py",
    "femto_bolt.launch.py",
    "femto_mega.launch.py",
    "gemini2.launch.py",
    "gemini210.launch.py",
    "gemini2L.launch.py",
    "gemini345.launch.py",
    "gemini345_lg.launch.py",
    "gemini435_le.launch.py",
    "gemini_301_series.launch.py",
    "gemini_330_series.launch.py",
    "gemini_330_series_low_cpu.launch.py",
    "gemini_330_series_sdk_json.launch.py",
  ],
  "1": [
    "astra.launch",
    "astra2.launch",
    "dabai_a.launch",
    "dabai_al.launch",
    "dabai_dcw2.launch",
    "dabai_max_pro.launch",
    "femto.launch",
    "femto_bolt.launch",
    "femto_mega.launch",
    "gemini2.launch",
    "gemini210.launch",
    "gemini2L.launch",
    "gemini345.launch",
    "gemini345_lg.launch",
    "gemini435_le.launch",
    "gemini_301_series.launch",
    "gemini_330_series.launch",
    "gemini_330_series_low_cpu.launch",
    "gemini_330_series_nodelet.launch",
    "gemini_330_series_nodelet_low_cpu.launch",
    "gemini_330_series_sdk_json.launch",
  ],
};
const SPECIAL_LAUNCH_CONFIGS = {
  "gemini_301_series.launch.py": [{ value: "dual_color", label: "Dual Color · 双彩色" }],
  "gemini_301_series.launch": [{ value: "dual_color", label: "Dual Color · 双彩色" }],
  "gemini2L.launch.py": [{ value: "dual_ir", label: "Dual IR · 双红外" }],
  "gemini2L.launch": [{ value: "dual_ir", label: "Dual IR · 双红外" }],
};
const STREAM_CONTROLS = {
  enable_color: "streamColor",
  enable_depth: "streamDepth",
  enable_ir: "streamIr",
  enable_left_ir: "streamLeftIr",
  enable_right_ir: "streamRightIr",
  enable_point_cloud: "streamPointCloud",
  enable_colored_point_cloud: "streamColoredPointCloud",
  enable_accel: "streamAccel",
  enable_gyro: "streamGyro",
  enable_sync_output_accel_gyro: "streamSyncImu",
};

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
    ros_version: $("rosVersion").value,
    ros_setup: $("rosSetup").value.trim(),
    camera_setup: $("cameraSetup").value.trim(),
    mode: $("mode").value,
    performance_scenario: $("performanceScenario").value,
    launch_config: $("launchConfig").value,
    stream_options: Object.fromEntries(
      Object.entries(STREAM_CONTROLS).map(([name, id]) => [name, $(id).value])
    ),
    run_count: $("runCount").value.trim(),
    continue_on_error: $("continueOnError").checked,
    duration: $("duration").value.trim(),
    stable_seconds: $("stableSeconds").value.trim(),
    stream_timeout: $("streamTimeout").value.trim(),
    max_gap_seconds: $("maxGapSeconds").value.trim(),
    restart_delay: $("restartDelay").value.trim(),
    image_topics: $("imageTopics").value,
    warning_interval_sec: $("warningIntervalSec").value.trim(),
    warmup_sec: $("warmupSec").value.trim(),
    save_csv: $("saveCsv").value,
    queue_size: $("queueSize").value.trim(),
    camera_name: $("cameraName").value.trim(),
    serial_number: $("serialNumber").value.trim(),
    usb_port: $("usbPort").value.trim(),
    config_file_path: $("configFilePath").value.trim(),
    launch_file: $("launchFile").value.trim(),
    launch_args: $("launchArgs").value,
  };
}

function truthy(value) {
  if (value === true) return true;
  return ["1", "true", "yes", "on"].includes(String(value || "").toLowerCase());
}

function setStatus(status) {
  const node = $("runStatus");
  const value = status || "idle";
  const dot = document.createElement("i");
  dot.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.textContent = value;
  node.replaceChildren(dot, label);
  node.className = `status-pill ${value}`;
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
  const shouldFollow = $("followLogs")?.checked ?? true;
  if (shouldFollow && atBottom) {
    log.scrollTop = log.scrollHeight;
  }
}

async function copyLogs() {
  const button = $("copyLogs");
  const logs = $("logOutput").textContent;
  if (!logs) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(logs);
    } else {
      const fallback = document.createElement("textarea");
      fallback.value = logs;
      fallback.style.position = "fixed";
      fallback.style.opacity = "0";
      document.body.appendChild(fallback);
      fallback.select();
      document.execCommand("copy");
      fallback.remove();
    }
    button.textContent = "已复制";
  } catch (error) {
    appendLogs([`[UI] copy failed: ${error.message}`]);
    button.textContent = "复制失败";
  }
  window.setTimeout(() => {
    button.textContent = "复制";
  }, 1400);
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

function updateLaunchConfigOptions(preferred = "") {
  const select = $("launchConfig");
  const options = SPECIAL_LAUNCH_CONFIGS[$("launchFile").value] || [];
  select.replaceChildren();
  const generic = document.createElement("option");
  generic.value = "generic";
  generic.textContent = "通用";
  select.appendChild(generic);
  for (const item of options) {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    select.appendChild(option);
  }
  if ([...select.options].some((option) => option.value === preferred)) {
    select.value = preferred;
  }
  $("launchConfigField").classList.toggle("is-hidden", options.length === 0);
  updateSpecialConfigControls();
}

function updateSpecialConfigControls() {
  const special = $("launchConfig").value !== "generic";
  for (const id of Object.values(STREAM_CONTROLS)) {
    $(id).disabled = special;
  }
  $("configFilePath").disabled = special;
  $("streamOptions").title = special ? "特殊配置 YAML 会覆盖普通流参数" : "";
}

function updateRosVersionControls({ fillBlank = false } = {}) {
  const defaults = DEFAULT_SETUPS[$("rosVersion").value] || DEFAULT_SETUPS["2"];
  $("rosSetup").placeholder = defaults.ros;
  $("cameraSetup").placeholder = defaults.cameraPlaceholder || defaults.camera;
  if (fillBlank) {
    if (!$("rosSetup").value.trim()) {
      $("rosSetup").value = defaults.ros;
    }
    if (!$("cameraSetup").value.trim()) {
      $("cameraSetup").value = defaults.camera;
    }
  }
  updateModeControls();
}

function updateModeControls() {
  const mode = $("mode").value;
  const needsPerformance = mode === "performance" || mode === "all";
  const needsPerformanceRuntime = mode === "performance" || mode === "restart" || mode === "stream_stall" || mode === "all";
  const needsRestart = mode === "restart";
  const needsStreamStall = mode === "stream_stall";
  const needsStreamTopics = needsRestart || needsStreamStall;
  $("performanceScenario").closest("label").classList.toggle("is-hidden", !needsPerformance);
  $("duration").closest("label").classList.toggle("is-hidden", !needsPerformanceRuntime);
  $("restartFields").classList.toggle("is-hidden", !needsRestart);
  $("streamStallFields").classList.toggle("is-hidden", !needsStreamStall);
  $("streamTopicFields").classList.toggle("is-hidden", !needsStreamTopics);

}

async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;
  $("rosVersion").value = config.ros_version || "2";
  $("rosSetup").value = config.ros_setup || "";
  $("cameraSetup").value = config.camera_setup || "";
  $("mode").value = config.mode || "functional";
  $("runCount").value = config.run_count || "1";
  $("continueOnError").checked = truthy(config.continue_on_error);
  $("duration").value = config.duration || "";
  $("stableSeconds").value = config.stable_seconds || "10";
  $("streamTimeout").value = config.stream_timeout || "60";
  $("maxGapSeconds").value = config.max_gap_seconds || "1.5";
  $("restartDelay").value = config.restart_delay || "2";
  $("imageTopics").value = config.image_topics || "";
  $("warningIntervalSec").value = config.warning_interval_sec || "1.0";
  $("warmupSec").value = config.warmup_sec || "2.0";
  $("saveCsv").value = config.save_csv || "true";
  $("queueSize").value = config.queue_size || "10";
  $("performanceScenario").value = config.performance_scenario || "";
  for (const [name, id] of Object.entries(STREAM_CONTROLS)) {
    $(id).value = config.stream_options?.[name] || "";
  }
  $("workspacePath").textContent = `工作区: ${config.auto_test_ws}`;
  updateRosVersionControls({ fillBlank: true });
}

function renderLaunchFileOptions(files, preferred = "") {
  const select = $("launchFile");
  select.replaceChildren();
  for (const launchFile of files) {
    const option = document.createElement("option");
    option.value = launchFile;
    option.textContent = launchFile;
    select.appendChild(option);
  }
  if (files.includes(preferred)) {
    select.value = preferred;
  }
}

function loadLaunchFiles() {
  const rosVersion = $("rosVersion").value || "2";
  const files = SINGLE_CAMERA_LAUNCH_FILES[rosVersion] || [];
  const configured = state.config.ros_version === rosVersion ? state.config.launch_file : "";
  renderLaunchFileOptions(files, configured);
  $("launchFile").title = `内置 ${files.length} 个 ROS${rosVersion} 单相机 Launch`;
  $("launchCount").textContent = `${files.length} · ROS ${rosVersion}`;
  updateLaunchConfigOptions(configured ? state.config.launch_config : "");
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
      $("currentRunId").textContent = "未运行";
      $("currentMode").textContent = "—";
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
      state.selectedRunId = null;
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
  item.className = `run-item${run.run_id === state.selectedRunId ? " selected" : ""}`;
  item.dataset.runId = run.run_id;

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
  state.selectedRunId = runId;
  for (const item of document.querySelectorAll(".run-item")) {
    item.classList.toggle("selected", item.dataset.runId === runId);
  }
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
  $("refreshLaunches").addEventListener("click", loadLaunchFiles);
  $("refreshRuns").addEventListener("click", loadRuns);
  $("copyLogs").addEventListener("click", copyLogs);
  $("clearLogs").addEventListener("click", () => {
    $("logOutput").textContent = "";
  });
  $("followLogs").addEventListener("change", () => {
    if ($("followLogs").checked) {
      $("logOutput").scrollTop = $("logOutput").scrollHeight;
    }
  });
  $("rosVersion").addEventListener("change", () => {
    const defaults = DEFAULT_SETUPS[$("rosVersion").value] || DEFAULT_SETUPS["2"];
    $("rosSetup").value = defaults.ros;
    $("cameraSetup").value = defaults.camera;
    updateRosVersionControls();
    loadLaunchFiles();
  });
  $("mode").addEventListener("change", updateModeControls);
  $("launchFile").addEventListener("change", () => updateLaunchConfigOptions());
  $("launchConfig").addEventListener("change", updateSpecialConfigControls);

  await loadConfig();
  loadLaunchFiles();
  await loadRuns();
  await pollStatus();
  state.polling = setInterval(pollStatus, 1000);
}

init().catch((error) => {
  setStatus("failed");
  appendLogs([`[UI] init failed: ${error.message}`]);
});

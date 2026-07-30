const state = {
  config: {},
  logOffset: 0,
  polling: null,
  selectedRunId: null,
  selectedRunIds: new Set(),
  workspace: "framework",
  standaloneTests: [],
  standaloneTest: null,
  setupDefaults: {},
  resultSummaryRunId: null,
  currentSnapshot: null,
  deviceQueryPending: false,
};

const $ = (id) => document.getElementById(id);
const MAX_VISIBLE_LOG_LINES = 500;
const THEME_STORAGE_KEY = "orbbec-ui-theme";
const THEME_COLORS = {
  light: "#f4f7fb",
  dark: "#07101d",
};
const ACTIVE_RUN_STATUSES = new Set(["starting", "running", "stopping"]);
const TERMINAL_RUN_STATUSES = new Set(["passed", "failed", "interrupted", "warning"]);
const STATUS_LABELS = {
  idle: "空闲",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  passed: "已通过",
  failed: "失败",
  interrupted: "已中断",
  warning: "警告",
};
const RESULT_SUMMARY_LABELS = {
  rounds: "运行轮次",
  successes: "成功次数",
  successful_restarts: "成功重启",
  launch_attempts: "启动轮次",
  passed_runs: "通过轮次",
  completed_runs: "完成轮次",
  passed_tests: "通过测试",
  completed_tests: "完成测试",
  topic_count: "Topic 数量",
  warning_count: "警告数量",
};
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

function applyTheme(theme, persist = true) {
  const nextTheme = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = nextTheme;
  document.querySelector('meta[name="theme-color"]').content = THEME_COLORS[nextTheme];
  for (const button of document.querySelectorAll("[data-theme-value]")) {
    button.setAttribute("aria-pressed", String(button.dataset.themeValue === nextTheme));
  }
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    } catch (_) {}
  }
}

function initTheme() {
  applyTheme(document.documentElement.dataset.theme, false);
  for (const button of document.querySelectorAll("[data-theme-value]")) {
    button.addEventListener("click", () => applyTheme(button.dataset.themeValue));
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    const message = payload.errors ? payload.errors.join("\n") : payload.error || response.statusText;
    const error = new Error(message);
    error.details = payload.errors || [payload.error || response.statusText];
    error.payload = payload;
    error.output = payload.output || "";
    throw error;
  }
  return payload;
}

function deviceQueryPayload() {
  if (state.workspace === "standalone") {
    const values = standaloneCurrentValues();
    const rosVersion = String(values.ros_version || "2");
    const defaults = state.setupDefaults[rosVersion] || {};
    return {
      ros_version: rosVersion,
      ros_domain_id: $("standaloneRosDomainId").value.trim(),
      ros_setup: values.ros_setup || defaults.ros_setup || "",
      camera_setup: values.driver_setup || defaults.driver_setup || "",
    };
  }
  return {
    ros_version: $("rosVersion").value,
    ros_domain_id: $("rosDomainId").value.trim(),
    ros_setup: $("rosSetup").value.trim(),
    camera_setup: $("cameraSetup").value.trim(),
  };
}

function deviceFact(label, value) {
  const fact = document.createElement("div");
  const name = document.createElement("span");
  const content = document.createElement("strong");
  name.textContent = label;
  content.textContent = value || "—";
  fact.append(name, content);
  return fact;
}

function devicePresetGroup(label, presets = []) {
  const group = document.createElement("div");
  group.className = "device-presets";
  const title = document.createElement("span");
  title.textContent = `${label} (${presets.length})`;
  const values = document.createElement("div");
  values.className = "device-preset-values";
  if (presets.length) {
    for (const preset of presets) {
      const chip = document.createElement("code");
      chip.textContent = preset;
      values.appendChild(chip);
    }
  } else {
    values.textContent = "—";
  }
  group.append(title, values);
  return group;
}

function renderDevices(payload) {
  const devices = payload.devices || [];
  const content = $("deviceInfoContent");
  content.replaceChildren();
  $("deviceInfoSummary").textContent = devices.length
    ? `检测到 ${devices.length} 台相机 · 查询耗时 ${payload.elapsed_seconds ?? "—"} 秒`
    : "未检测到相机，请检查连接、权限和 Camera ROS Setup。";

  if (!devices.length) {
    const empty = document.createElement("div");
    empty.className = "device-info-state";
    empty.innerHTML = "<span>◎</span><strong>未发现设备</strong><p>可点击刷新重新查询。</p>";
    content.appendChild(empty);
  }
  devices.forEach((device, index) => {
    const card = document.createElement("article");
    card.className = "device-card";
    const header = document.createElement("header");
    const order = document.createElement("span");
    const title = document.createElement("div");
    order.textContent = String(index + 1).padStart(2, "0");
    title.innerHTML = "<small>ORBBEC CAMERA</small><h3></h3>";
    title.querySelector("h3").textContent = device.name || `相机 ${index + 1}`;
    header.append(order, title);

    const facts = document.createElement("div");
    facts.className = "device-facts";
    facts.append(
      deviceFact("PID", device.pid),
      deviceFact("Serial", device.serial),
      deviceFact("Connection", device.connection),
      deviceFact("Firmware", device.firmware_version),
      deviceFact("USB Port", device.usb_port),
      deviceFact("Preset Version", device.preset_version)
    );
    card.append(
      header,
      facts,
      devicePresetGroup("Device Presets", device.device_presets),
      devicePresetGroup("Color Presets", device.color_presets)
    );
    content.appendChild(card);
  });

  const raw = $("deviceRawOutput");
  raw.classList.toggle("is-hidden", !payload.output);
  raw.querySelector("pre").textContent = payload.output || "";
}

async function refreshDevices() {
  if (state.deviceQueryPending) return;
  state.deviceQueryPending = true;
  const button = $("refreshDevices");
  const content = $("deviceInfoContent");
  button.disabled = true;
  button.textContent = "查询中…";
  $("deviceInfoSummary").textContent = "正在运行 list_devices_node，请稍候…";
  content.innerHTML = '<div class="device-info-state loading"><span>◌</span><strong>正在查询相机</strong></div>';
  $("deviceRawOutput").classList.add("is-hidden");
  try {
    const payload = await api("/api/devices", {
      method: "POST",
      body: JSON.stringify(deviceQueryPayload()),
    });
    renderDevices(payload);
  } catch (error) {
    $("deviceInfoSummary").textContent = "相机信息查询失败";
    content.replaceChildren();
    const failure = document.createElement("div");
    failure.className = "device-info-state error";
    const mark = document.createElement("span");
    const title = document.createElement("strong");
    const detail = document.createElement("p");
    mark.textContent = "×";
    title.textContent = "无法获取设备信息";
    detail.textContent = error.message;
    failure.append(mark, title, detail);
    content.appendChild(failure);
    const raw = $("deviceRawOutput");
    raw.classList.toggle("is-hidden", !error.output);
    raw.querySelector("pre").textContent = error.output || "";
  } finally {
    state.deviceQueryPending = false;
    button.disabled = false;
    button.innerHTML = '<span aria-hidden="true">↻</span> 刷新';
  }
}

function showDeviceInfo() {
  const dialog = $("deviceInfoDialog");
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
  refreshDevices();
}

function formPayload() {
  return {
    ros_version: $("rosVersion").value,
    ros_domain_id: $("rosDomainId").value.trim(),
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

const CAMERA_FIELD_LABELS = {
  name: "Camera Name",
  "serial-number": "Serial Number",
  "usb-port": "USB Port",
  "device-ip": "Device IP",
  "device-port": "Device Port",
  "config-file-path": "Config File Path",
};
const CAMERA_FIELDS_BY_KIND = {
  usb: ["name", "serial-number", "usb-port", "config-file-path"],
  network: ["name", "device-ip", "device-port", "config-file-path"],
};

function choiceParts(choice) {
  if (typeof choice === "object") {
    return [String(choice.value ?? ""), String(choice.label ?? choice.value ?? "")];
  }
  return [String(choice), String(choice)];
}

function cameraKind(camera = {}) {
  if (camera["device-ip"] || camera["device-port"]) return "network";
  if (camera["serial-number"] || camera["usb-port"]) return "usb";
  const hasNetworkFields =
    Object.hasOwn(camera, "device-ip") || Object.hasOwn(camera, "device-port");
  const hasUsbFields =
    Object.hasOwn(camera, "serial-number") || Object.hasOwn(camera, "usb-port");
  return hasNetworkFields && !hasUsbFields ? "network" : "usb";
}

function cameraHasValues(camera = {}) {
  return Object.values(camera).some((value) => String(value || "").trim());
}

function addCameraRow(container, kind, camera = {}) {
  const row = document.createElement("div");
  row.className = "camera-row";
  row.dataset.cameraKind = kind;
  for (const name of CAMERA_FIELDS_BY_KIND[kind]) {
    const label = document.createElement("label");
    const title = document.createElement("span");
    title.textContent = CAMERA_FIELD_LABELS[name];
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.cameraField = name;
    input.value = camera[name] || "";
    label.append(title, input);
    row.appendChild(label);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "camera-remove";
  remove.textContent = "移除";
  remove.addEventListener("click", () => {
    row.remove();
    updateFormReadiness();
  });
  row.appendChild(remove);
  container
    .querySelector(`.camera-group[data-camera-kind="${kind}"] .camera-rows`)
    .appendChild(row);
}

function createCameraGroup(kind, titleText, hintText) {
  const group = document.createElement("section");
  group.className = "camera-group";
  group.dataset.cameraKind = kind;
  const heading = document.createElement("div");
  heading.className = "camera-group-title";
  const title = document.createElement("strong");
  title.textContent = titleText;
  const hint = document.createElement("small");
  hint.textContent = hintText;
  heading.append(title, hint);
  const rows = document.createElement("div");
  rows.className = "camera-rows";
  rows.dataset.emptyText = `尚未添加${titleText}`;
  group.append(heading, rows);
  return group;
}

function createStandaloneField(field, value) {
  const wrapper = document.createElement("div");
  wrapper.className = "standalone-field";
  wrapper.dataset.fieldName = field.name;
  wrapper.dataset.fieldType = field.type;
  if (field.when) wrapper.dataset.when = JSON.stringify(field.when);

  if (field.type === "camera-list") {
    wrapper.classList.add("grid-span-2");
    const header = document.createElement("div");
    header.className = "camera-editor-header";
    const title = document.createElement("strong");
    title.textContent = field.label;
    if (field.required) title.classList.add("required-label");
    const actions = document.createElement("div");
    actions.className = "camera-add-actions";
    const addUsb = document.createElement("button");
    addUsb.type = "button";
    addUsb.className = "ghost";
    addUsb.textContent = "＋ 添加 USB 相机";
    const addNetwork = document.createElement("button");
    addNetwork.type = "button";
    addNetwork.className = "ghost";
    addNetwork.textContent = "＋ 添加网络相机";
    actions.append(addUsb, addNetwork);
    header.append(title, actions);
    const editor = document.createElement("div");
    editor.className = "camera-editor";
    editor.append(
      createCameraGroup("usb", "USB 相机", "Serial Number / USB Port"),
      createCameraGroup("network", "网络相机", "Device IP / Device Port")
    );
    wrapper.append(header, editor);
    const cameras = Array.isArray(value) ? value.filter(cameraHasValues) : [];
    cameras.forEach((camera) => addCameraRow(editor, cameraKind(camera), camera));
    addUsb.addEventListener("click", () => {
      addCameraRow(editor, "usb");
      updateFormReadiness();
    });
    addNetwork.addEventListener("click", () => {
      addCameraRow(editor, "network");
      updateFormReadiness();
    });
    return wrapper;
  }

  const label = document.createElement("label");
  const title = document.createElement("span");
  title.textContent = field.label || field.name;
  if (field.required) title.classList.add("required-label");
  let control;
  if (field.type === "select") {
    control = document.createElement("select");
    for (const choice of field.choices || []) {
      const [choiceValue, choiceLabel] = choiceParts(choice);
      const option = document.createElement("option");
      option.value = choiceValue;
      option.textContent = choiceLabel;
      control.appendChild(option);
    }
    control.value = String(value ?? "");
  } else if (field.type === "list") {
    control = document.createElement("textarea");
    control.rows = 3;
    control.value = Array.isArray(value) ? value.join("\n") : String(value || "");
    control.placeholder = "每行填写一个值";
  } else if (field.type === "flag" || field.type === "boolean") {
    label.classList.add("checkbox-field", "standalone-checkbox");
    control = document.createElement("input");
    control.type = "checkbox";
    control.checked = Boolean(value);
  } else {
    control = document.createElement("input");
    control.type = field.type === "integer" || field.type === "number" ? "number" : "text";
    if (field.type === "number") control.step = "any";
    if (field.type === "integer") control.step = "1";
    if (field.min !== undefined) control.min = String(field.min);
    if (field.max !== undefined) control.max = String(field.max);
    control.value = String(value ?? "");
    if (field.type === "duration") control.placeholder = "300 / 15m / 2h";
  }
  control.dataset.standaloneInput = field.name;
  if (field.required) control.required = true;
  label.append(title, control);
  wrapper.appendChild(label);
  return wrapper;
}

function standaloneCurrentValues() {
  const values = {};
  if (!state.standaloneTest) return values;
  for (const field of state.standaloneTest.fields || []) {
    const wrapper = document.querySelector(`.standalone-field[data-field-name="${field.name}"]`);
    if (!wrapper || wrapper.classList.contains("is-hidden")) continue;
    if (field.type === "camera-list") {
      values[field.name] = [...wrapper.querySelectorAll(".camera-row")].map((row) =>
        Object.fromEntries(
          [...row.querySelectorAll("[data-camera-field]")].map((input) => [
            input.dataset.cameraField,
            input.value.trim(),
          ])
        )
      );
      continue;
    }
    const control = wrapper.querySelector("[data-standalone-input]");
    if (field.type === "flag" || field.type === "boolean") {
      values[field.name] = control.checked;
    } else if (field.type === "list") {
      values[field.name] = control.value.split("\n").map((item) => item.trim()).filter(Boolean);
    } else {
      values[field.name] = control.value.trim();
    }
  }
  return values;
}

function updateStandaloneConditions() {
  const values = {};
  for (const control of document.querySelectorAll("[data-standalone-input]")) {
    values[control.dataset.standaloneInput] =
      control.type === "checkbox" ? control.checked : control.value;
  }
  for (const wrapper of document.querySelectorAll(".standalone-field[data-when]")) {
    const condition = JSON.parse(wrapper.dataset.when);
    const visible = Object.entries(condition).every(
      ([name, expected]) => String(values[name] ?? "") === String(expected)
    );
    wrapper.classList.toggle("is-hidden", !visible);
  }
  updateFormReadiness();
}

function setupDefaultsForVersion(rosVersion) {
  const fallback = DEFAULT_SETUPS[rosVersion] || DEFAULT_SETUPS["2"];
  const configured = state.setupDefaults?.[rosVersion] || {};
  return {
    ros: configured.ros_setup || fallback.ros,
    camera: configured.driver_setup || fallback.camera,
    cameraPlaceholder: fallback.cameraPlaceholder,
  };
}

function launchFileForRosVersion(launchFile, rosVersion) {
  const value = String(launchFile || "").trim();
  if (rosVersion === "1" && value.endsWith(".launch.py")) {
    return value.slice(0, -3);
  }
  if (rosVersion === "2" && value.endsWith(".launch")) {
    return `${value}.py`;
  }
  return value;
}

function updateStandaloneRosVersion(rosVersion) {
  const defaults = setupDefaultsForVersion(rosVersion);
  const rosSetup = document.querySelector(
    '[data-standalone-input="ros_setup"]'
  );
  const driverSetup = document.querySelector(
    '[data-standalone-input="driver_setup"]'
  );
  const launchFile = document.querySelector(
    '[data-standalone-input="launch_file"]'
  );
  if (rosSetup) {
    rosSetup.value = defaults.ros;
    rosSetup.placeholder = defaults.ros;
  }
  if (driverSetup) {
    driverSetup.value = defaults.camera;
    driverSetup.placeholder = defaults.cameraPlaceholder || defaults.camera;
  }
  if (launchFile) {
    launchFile.value = launchFileForRosVersion(launchFile.value, rosVersion);
  }
  updateStandaloneDomainControl(rosVersion);
}

function updateStandaloneDomainControl(rosVersion) {
  const control = $("standaloneRosDomainId");
  const enabled = String(rosVersion) === "2";
  control.disabled = !enabled;
  control.title = enabled
    ? "ROS 2 Domain ID，留空表示不设置"
    : "ROS 1 不使用 Domain ID";
}

function renderStandaloneForm(testId = "") {
  const test =
    state.standaloneTests.find((item) => item.id === testId) || state.standaloneTests[0];
  state.standaloneTest = test || null;
  const basic = $("standaloneBasicFields");
  const advanced = $("standaloneAdvancedFields");
  basic.replaceChildren();
  advanced.replaceChildren();
  if (!test) {
    $("standaloneDescription").textContent = "没有找到可用的独立脚本清单。";
    return;
  }
  $("standaloneTest").value = test.id;
  $("standaloneDescription").textContent = test.description || "";
  $("standaloneRisk").textContent = test.confirmation || "";
  $("standaloneRisk").classList.toggle("is-hidden", test.risk !== "high");
  for (const field of test.fields || []) {
    const target = field.section === "advanced" ? advanced : basic;
    target.appendChild(createStandaloneField(field, test.values?.[field.name]));
  }
  for (const control of document.querySelectorAll("[data-standalone-input]")) {
    control.addEventListener("change", () => {
      if (control.dataset.standaloneInput === "ros_version") {
        updateStandaloneRosVersion(control.value);
      }
      updateStandaloneConditions();
    });
  }
  updateStandaloneDomainControl(
    document.querySelector('[data-standalone-input="ros_version"]')?.value || "2"
  );
  updateStandaloneConditions();
  clearValidation("standaloneForm", "standaloneValidation");
}

async function loadStandaloneTests() {
  const payload = await api("/api/standalone/tests");
  state.setupDefaults = {
    ...state.setupDefaults,
    ...(payload.setup_defaults || {}),
  };
  state.standaloneTests = payload.tests || [];
  const select = $("standaloneTest");
  select.replaceChildren();
  for (const test of state.standaloneTests) {
    const option = document.createElement("option");
    option.value = test.id;
    option.textContent = test.title;
    select.appendChild(option);
  }
  renderStandaloneForm(select.value);
  if (state.workspace === "standalone") switchWorkspace("standalone");
}

function truthy(value) {
  if (value === true) return true;
  return ["1", "true", "yes", "on"].includes(String(value || "").toLowerCase());
}

const DURATION_PATTERN = /^\d+(?:\.\d+)?[smh]?$/i;
const FRAMEWORK_ERROR_FIELDS = [
  ["domain id", "rosDomainId"],
  ["camera ros setup", "cameraSetup"],
  ["ros setup", "rosSetup"],
  ["launch_file", "launchFile"],
  ["launch config", "launchConfig"],
  ["launch arg", "launchArgs"],
  ["run_count", "runCount"],
  ["stable_seconds", "stableSeconds"],
  ["stream_timeout", "streamTimeout"],
  ["max_gap_seconds", "maxGapSeconds"],
  ["restart_delay", "restartDelay"],
  ["warning_interval_sec", "warningIntervalSec"],
  ["warmup_sec", "warmupSec"],
  ["queue_size", "queueSize"],
  ["duration", "duration"],
];

function validationIssue(target, message) {
  return { target, message };
}

function validateDurationControl(control, { required = false } = {}) {
  const value = control.value.trim();
  if (!value) {
    return required ? validationIssue(control, `${control.closest("label")?.querySelector("span")?.textContent || "运行时长"}为必填项`) : null;
  }
  if (!DURATION_PATTERN.test(value)) {
    return validationIssue(control, "请输入秒数，或使用 s / m / h 后缀，例如 300、15m");
  }
  return null;
}

function validateNumberControl(
  control,
  { integer = false, min = null, max = null, strictMin = false } = {}
) {
  const value = control.value.trim();
  if (!value) return null;
  const number = Number(value);
  const label = control.closest("label")?.querySelector("span")?.textContent || "数值";
  if (!Number.isFinite(number) || (integer && !Number.isInteger(number))) {
    return validationIssue(control, `${label}必须是${integer ? "整数" : "数字"}`);
  }
  if (min !== null && (strictMin ? number <= min : number < min)) {
    return validationIssue(control, `${label}必须${strictMin ? "大于" : "大于或等于"} ${min}`);
  }
  if (max !== null && number > max) {
    return validationIssue(control, `${label}必须小于或等于 ${max}`);
  }
  return null;
}

function validateFrameworkForm() {
  const errors = [];
  if (!$("rosSetup").value.trim()) {
    errors.push(validationIssue($("rosSetup"), "ROS Setup 为必填项"));
  }
  if (!$("launchFile").value.trim()) {
    errors.push(validationIssue($("launchFile"), "请选择 Launch 文件"));
  }
  const domainError = validateNumberControl($("rosDomainId"), {
    integer: true,
    min: 0,
    max: 232,
  });
  if (domainError) errors.push(domainError);
  const runCountError = validateNumberControl($("runCount"), { integer: true, min: 0, strictMin: true });
  if (runCountError) errors.push(runCountError);

  const mode = $("mode").value;
  const durationError = validateDurationControl($("duration"), {
    required: mode === "restart" || mode === "stream_stall",
  });
  if (durationError) errors.push(durationError);
  for (const id of ["stableSeconds", "streamTimeout", "maxGapSeconds", "warmupSec"]) {
    if (!$(id).closest(".is-hidden")) {
      const error = validateDurationControl($(id));
      if (error) errors.push(error);
    }
  }
  for (const [id, options] of [
    ["restartDelay", { min: 0 }],
    ["warningIntervalSec", { min: 0, strictMin: true }],
    ["queueSize", { integer: true, min: 0, strictMin: true }],
  ]) {
    if (!$(id).closest(".is-hidden")) {
      const error = validateNumberControl($(id), options);
      if (error) errors.push(error);
    }
  }

  const invalidLaunchArg = $("launchArgs").value
    .split("\n")
    .map((item) => item.trim())
    .find((item) => item && (!item.includes("=") || !item.split("=", 1)[0].trim()));
  if (invalidLaunchArg) {
    errors.push(validationIssue($("launchArgs"), `Launch 参数必须使用 KEY=VALUE：${invalidLaunchArg}`));
  }
  return errors;
}

function meaningfulCameraRows(wrapper) {
  return [...wrapper.querySelectorAll(".camera-row")].filter((row) =>
    [...row.querySelectorAll("[data-camera-field]")].some((input) => input.value.trim())
  );
}

function validateStandaloneForm() {
  const errors = [];
  if (!state.standaloneTest) {
    return [validationIssue($("standaloneTest"), "请选择一个独立脚本")];
  }
  const domainError = validateNumberControl($("standaloneRosDomainId"), {
    integer: true,
    min: 0,
    max: 232,
  });
  if (domainError) errors.push(domainError);
  for (const field of state.standaloneTest.fields || []) {
    const wrapper = document.querySelector(`.standalone-field[data-field-name="${field.name}"]`);
    if (!wrapper || wrapper.classList.contains("is-hidden")) continue;
    if (field.type === "camera-list") {
      const rows = meaningfulCameraRows(wrapper);
      if (field.required && !rows.length) {
        errors.push(validationIssue(wrapper, `${field.label || field.name}至少需要添加一台相机`));
      }
      if (field.max_items !== undefined && rows.length > Number(field.max_items)) {
        errors.push(validationIssue(wrapper, `${field.label || field.name}最多允许 ${field.max_items} 台相机`));
      }
      if (field.config_file_required) {
        rows.forEach((row, index) => {
          const configInput = row.querySelector('[data-camera-field="config-file-path"]');
          if (configInput && !configInput.value.trim()) {
            errors.push(validationIssue(configInput, `相机 ${index + 1} 必须填写 Config File Path`));
          }
        });
      }
      continue;
    }

    const control = wrapper.querySelector("[data-standalone-input]");
    const label = field.label || field.name;
    const value = field.type === "list"
      ? control.value.split("\n").map((item) => item.trim()).filter(Boolean)
      : field.type === "flag" || field.type === "boolean"
        ? control.checked
        : control.value.trim();
    const empty = value === "" || value === false || (Array.isArray(value) && !value.length);
    if (field.required && empty) {
      errors.push(validationIssue(control, `${label}为必填项`));
      continue;
    }
    if (empty) continue;
    if (field.type === "duration") {
      const error = validateDurationControl(control);
      if (error) errors.push(error);
    } else if (field.type === "integer" || field.type === "number") {
      const error = validateNumberControl(control, {
        integer: field.type === "integer",
        min: field.min ?? null,
      });
      if (error) errors.push(error);
      const number = Number(control.value);
      if (field.max !== undefined && Number.isFinite(number) && number > Number(field.max)) {
        errors.push(validationIssue(control, `${label}必须小于或等于 ${field.max}`));
      }
    }
  }
  return errors;
}

function clearValidation(formId, summaryId) {
  const form = $(formId);
  form.querySelectorAll("[aria-invalid='true']").forEach((node) => node.removeAttribute("aria-invalid"));
  form.querySelectorAll(".field-invalid").forEach((node) => node.classList.remove("field-invalid"));
  form.querySelectorAll(".field-error").forEach((node) => node.remove());
  const summary = $(summaryId);
  summary.replaceChildren();
  summary.classList.add("is-hidden");
}

function presentValidationErrors(formId, summaryId, errors, { focus = true } = {}) {
  clearValidation(formId, summaryId);
  if (!errors.length) return true;
  const unique = errors.filter(
    (error, index) => errors.findIndex((item) => item.message === error.message) === index
  );
  const summary = $(summaryId);
  const title = document.createElement("strong");
  title.textContent = `还有 ${unique.length} 项需要检查`;
  const list = document.createElement("ul");
  for (const error of unique) {
    const item = document.createElement("li");
    item.textContent = error.message;
    list.appendChild(item);
    const target = error.target;
    if (!target) continue;
    target.closest("details")?.setAttribute("open", "");
    const control = target.matches?.("input, select, textarea")
      ? target
      : target.querySelector?.("input, select, textarea");
    const host = control?.closest("label") || target;
    host.classList.add("field-invalid");
    if (control) control.setAttribute("aria-invalid", "true");
    const note = document.createElement("small");
    note.className = "field-error";
    note.textContent = error.message;
    host.appendChild(note);
  }
  summary.append(title, list);
  summary.classList.remove("is-hidden");
  if (focus) {
    const target = unique[0]?.target;
    const control = target?.matches?.("input, select, textarea")
      ? target
      : target?.querySelector?.("input, select, textarea");
    (control || target)?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    control?.focus({ preventScroll: true });
  }
  return false;
}

function serverValidationIssues(error, standalone = false) {
  const messages = Array.isArray(error.details) ? error.details : [error.message];
  return messages.filter(Boolean).map((message) => {
    const normalized = String(message).toLowerCase();
    let target = null;
    if (standalone) {
      if (normalized.includes("domain id")) {
        target = $("standaloneRosDomainId");
      }
      const field = (state.standaloneTest?.fields || []).find((item) =>
        normalized.includes(String(item.label || item.name).toLowerCase()) ||
        normalized.includes(String(item.name).toLowerCase())
      );
      const wrapper = field
        ? document.querySelector(`.standalone-field[data-field-name="${field.name}"]`)
        : null;
      target = target || wrapper?.querySelector("input, select, textarea") || wrapper;
    } else {
      const match = FRAMEWORK_ERROR_FIELDS.find(([hint]) => normalized.includes(hint));
      target = match ? $(match[1]) : null;
    }
    return validationIssue(target, String(message));
  });
}

function updateFormReadiness() {
  for (const [workspace, errors, node] of [
    ["framework", validateFrameworkForm(), $("frameworkReadiness")],
    ["standalone", validateStandaloneForm(), $("standaloneReadiness")],
  ]) {
    const ready = errors.length === 0;
    node.classList.toggle("ready", ready);
    node.classList.toggle("needs-attention", !ready);
    node.querySelector("span").textContent = ready
      ? "必填项已完整"
      : `还需完善 ${errors.length} 项`;
    node.title = workspace === "framework" ? "测试框架配置检查" : "独立脚本配置检查";
  }
}

function setStatus(status) {
  const node = $("runStatus");
  const value = status || "idle";
  const dot = document.createElement("i");
  dot.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.textContent = STATUS_LABELS[value] || value;
  node.replaceChildren(dot, label);
  node.className = `status-pill ${value}`;
  const running = ACTIVE_RUN_STATUSES.has(value);
  $("startButton").disabled = running;
  $("stopButton").disabled = !running;
  $("stopButton").classList.toggle("is-hidden", !running);
  $("standaloneStartButton").disabled = running;
  $("standaloneStopButton").disabled = !running;
  $("standaloneStopButton").classList.toggle("is-hidden", !running);
  $("showDevices").disabled = running;
  $("showDevices").title = running ? "测试运行期间暂停设备枚举" : "查看已连接的 ROS 2 相机";

  const showMonitor = value !== "idle";
  $("monitorEmpty").classList.toggle("is-hidden", showMonitor);
  $("monitorContent").classList.toggle("is-hidden", !showMonitor);
  const monitor = $("monitorEmpty").closest(".monitor");
  monitor.dataset.state = value;
  monitor.dataset.terminal = String(TERMINAL_RUN_STATUSES.has(value));
  if (!TERMINAL_RUN_STATUSES.has(value)) {
    $("runResultSummary").classList.add("is-hidden");
  }
}

function updateMonitorEmptyCopy() {
  const standalone = state.workspace === "standalone";
  $("monitorEmptyTitle").textContent = standalone ? "等待独立脚本" : "等待测试任务";
  $("monitorEmptyDescription").textContent = standalone
    ? "选择一个独立工具并检查必要参数，启动指令、运行进度与日志会显示在这里。"
    : "在左侧完成环境与测试策略配置，运行指标、数据流与实时日志会显示在这里。";
}

function switchWorkspace(workspace, updateUrl = true) {
  state.workspace = workspace === "standalone" ? "standalone" : "framework";
  $("frameworkWorkspaceButton").classList.toggle("active", state.workspace === "framework");
  $("standaloneWorkspaceButton").classList.toggle("active", state.workspace === "standalone");
  $("frameworkWorkspaceButton").setAttribute(
    "aria-pressed",
    String(state.workspace === "framework")
  );
  $("standaloneWorkspaceButton").setAttribute(
    "aria-pressed",
    String(state.workspace === "standalone")
  );
  for (const panel of document.querySelectorAll("[data-workspace-panel]")) {
    panel.classList.toggle("is-hidden", panel.dataset.workspacePanel !== state.workspace);
  }
  $("launchCount").textContent =
    state.workspace === "standalone"
      ? `${state.standaloneTests.length} 个脚本`
      : $("launchFile").options.length
        ? `${$("launchFile").options.length} 个 · ROS ${$("rosVersion").value}`
        : "—";
  $("resourceCountLabel").textContent =
    state.workspace === "standalone" ? "可用脚本" : "可用 Launch";
  $("currentModeLabel").textContent =
    state.workspace === "standalone" ? "任务类型" : "测试模式";
  updateMonitorEmptyCopy();
  if (updateUrl) {
    const url = new URL(window.location.href);
    if (state.workspace === "standalone") {
      url.searchParams.set("workspace", "standalone");
    } else {
      url.searchParams.delete("workspace");
    }
    window.history.replaceState({}, "", url);
  }
}

function renderCommands(commands = []) {
  const box = $("commandBox");
  const signature = JSON.stringify(commands);
  if (box.dataset.commands === signature) return;
  box.dataset.commands = signature;
  box.replaceChildren();
  box.open = false;
  if (!commands.length) return;

  const summary = document.createElement("summary");
  const label = document.createElement("span");
  label.className = "command-label";
  label.textContent = "启动命令";
  const preview = document.createElement("code");
  preview.className = "command-preview";
  preview.textContent = commands[0];
  const toggle = document.createElement("span");
  toggle.className = "command-toggle";
  summary.append(label, preview, toggle);

  const lines = document.createElement("div");
  lines.className = "command-lines";
  for (const command of commands) {
    const line = document.createElement("div");
    line.className = "command-line";
    line.textContent = command;
    lines.appendChild(line);
  }
  box.append(summary, lines);
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

function displayResultValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function resultSummaryLabel(key) {
  return RESULT_SUMMARY_LABELS[key] || String(key).replaceAll("_", " ");
}

function resultStatusPresentation(status) {
  return {
    passed: { mark: "✓", kicker: "RUN COMPLETE", title: "测试已通过", message: "所有已报告的检查均已完成。" },
    warning: { mark: "!", kicker: "COMPLETED WITH WARNINGS", title: "测试完成，有警告", message: "任务已完成，请检查警告信息和报告。" },
    failed: { mark: "×", kicker: "RUN FAILED", title: "测试未通过", message: "任务执行失败，请检查摘要和实时日志。" },
    interrupted: { mark: "■", kicker: "RUN INTERRUPTED", title: "测试已中断", message: "任务已停止，已生成的数据仍可在报告中查看。" },
  }[status] || { mark: "·", kicker: "RUN COMPLETE", title: "测试已完成", message: "查看本次任务的运行结果。" };
}

function showExitCodeHelp() {
  const dialog = $("exitCodeDialog");
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}

function resultPayloadFrom(snapshot, detail = {}) {
  const results = Object.values(detail.results || {}).filter(
    (item) => item && typeof item === "object"
  );
  if (snapshot.runner_type === "standalone") {
    return results[0] || snapshot.standalone?.result || {};
  }
  return results.find((item) => item.status === "failed") || results[0] || {};
}

function renderResultSummary(snapshot, detail = {}) {
  const panel = $("runResultSummary");
  if (!TERMINAL_RUN_STATUSES.has(snapshot.status) || !snapshot.run_id) {
    panel.classList.add("is-hidden");
    return;
  }

  const presentation = resultStatusPresentation(snapshot.status);
  const result = resultPayloadFrom(snapshot, detail);
  const results = Object.values(detail.results || {}).filter(
    (item) => item && typeof item === "object"
  );
  const elapsed = snapshot.standalone?.elapsed_seconds ?? snapshot.performance?.elapsed_seconds;
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  const errorValue = result.error?.message || result.error;

  panel.dataset.status = snapshot.status;
  $("runResultMark").textContent = presentation.mark;
  panel.querySelector(".panel-kicker").textContent = presentation.kicker;
  $("runResultTitle").textContent = presentation.title;
  $("runResultMessage").textContent = errorValue
    ? displayResultValue(errorValue)
    : warnings.length
      ? displayResultValue(warnings[0])
      : presentation.message;

  const facts = [
    ["运行时长", formatDuration(elapsed)],
    ["退出码", snapshot.exit_code ?? "—"],
    ["结果文件", results.length || (Object.keys(result).length ? 1 : "—")],
    ["结束时间", snapshot.ended_at ? snapshot.ended_at.replace("T", " ") : "—"],
  ];
  const factContainer = $("runResultFacts");
  factContainer.replaceChildren();
  for (const [labelText, value] of facts) {
    const fact = document.createElement("div");
    const label = document.createElement("span");
    const strong = document.createElement("strong");
    label.textContent = labelText;
    if (labelText === "退出码") {
      const help = document.createElement("button");
      help.type = "button";
      help.className = "exit-code-help";
      help.textContent = "?";
      help.title = "查看所有退出码的含义";
      help.setAttribute("aria-label", "查看所有退出码的含义");
      help.addEventListener("click", showExitCodeHelp);
      label.appendChild(help);
    }
    strong.textContent = displayResultValue(value);
    fact.append(label, strong);
    factContainer.appendChild(fact);
  }

  const highlights = [];
  for (const [key, value] of Object.entries(result.summary || {})) {
    highlights.push([resultSummaryLabel(key), displayResultValue(value)]);
  }
  if (!highlights.length && snapshot.runner_type !== "standalone") {
    const statuses = results.map((item) => item.status).filter(Boolean);
    if (statuses.length) {
      highlights.push(
        ["通过结果", statuses.filter((status) => status === "passed").length],
        ["失败结果", statuses.filter((status) => status === "failed").length]
      );
    }
    const topics = snapshot.performance?.fps_topics || [];
    if (topics.length) highlights.push(["数据流", `${topics.length} 个 Topic`]);
  }
  if (warnings.length) highlights.push(["警告", `${warnings.length} 条`]);

  const highlightContainer = $("runResultHighlights");
  highlightContainer.replaceChildren();
  for (const [labelText, value] of highlights.slice(0, 6)) {
    const item = document.createElement("div");
    const label = document.createElement("span");
    const strong = document.createElement("strong");
    label.textContent = labelText;
    strong.textContent = displayResultValue(value);
    strong.title = displayResultValue(value);
    item.append(label, strong);
    highlightContainer.appendChild(item);
  }
  highlightContainer.classList.toggle("is-hidden", !highlightContainer.children.length);
  panel.classList.remove("is-hidden");
}

async function updateResultSummary(snapshot) {
  state.currentSnapshot = snapshot;
  if (!TERMINAL_RUN_STATUSES.has(snapshot.status) || !snapshot.run_id) {
    $("runResultSummary").classList.add("is-hidden");
    state.resultSummaryRunId = null;
    return;
  }
  renderResultSummary(snapshot);
  if (state.resultSummaryRunId === snapshot.run_id) return;
  try {
    const detail = await api(`/api/runs/${encodeURIComponent(snapshot.run_id)}`);
    if (state.currentSnapshot?.run_id === snapshot.run_id) {
      renderResultSummary(snapshot, detail);
    }
    if (
      TERMINAL_RUN_STATUSES.has(detail.ui_status?.status) ||
      Object.keys(detail.results || {}).length
    ) {
      state.resultSummaryRunId = snapshot.run_id;
    }
  } catch (error) {
    appendLogs([`[UI] result summary load failed: ${error.message}`]);
  }
}

function renderPerformance(performance = {}) {
  const reportedScopes = performance.system_scopes || [];
  const hasSystemData =
    reportedScopes.length > 0 ||
    Number(performance.cpu_percent) > 0 ||
    Number(performance.memory_rss_mb) > 0 ||
    Number(performance.pid_count) > 0;
  $("perfElapsed").textContent = formatDuration(performance.elapsed_seconds);
  $("perfCpu").textContent = hasSystemData
    ? `${formatNumber(performance.cpu_percent, 1)}%`
    : "--";
  $("perfRam").textContent = hasSystemData
    ? `${formatNumber(performance.memory_rss_mb, 1)} MB`
    : "--";
  $("perfPidCount").textContent = hasSystemData
    ? String(performance.pid_count || 0)
    : "--";

  const systemBody = $("systemTableBody");
  systemBody.innerHTML = "";
  const detailScopes = reportedScopes.filter(
    (scope) => !(scope.scope === "total" && scope.camera_name === "all")
  );
  const scopes = detailScopes.length
    ? detailScopes
    : hasSystemData
      ? [
          {
            label: "总计",
            cpu_percent: performance.cpu_percent,
            memory_rss_mb: performance.memory_rss_mb,
            pid_count: performance.pid_count,
          },
        ]
      : [];
  if (!scopes.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.textContent = "等待资源采样。";
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

function formatStandaloneRound(progress = {}) {
  if (!progress.supported) return "—";
  const current = Math.max(0, Number(progress.current) || 0);
  const total = Number(progress.total);
  return Number.isFinite(total) && total > 0 ? `${current} / ${total}` : String(current);
}

function renderMonitor(payload = {}) {
  const standalone = payload.runner_type === "standalone";
  $("standaloneMonitor").classList.toggle("is-hidden", !standalone);
  $("frameworkMonitor").classList.toggle("is-hidden", standalone);
  $("runMeta").classList.toggle("is-hidden", standalone);
  const statusLabel = STATUS_LABELS[payload.status] || payload.status || "";
  $("monitorDescription").textContent = standalone
    ? `独立脚本进度与实时日志${statusLabel ? ` · ${statusLabel}` : ""}`
    : `进程资源、数据流与实时日志${statusLabel ? ` · ${statusLabel}` : ""}`;

  if (standalone) {
    $("standaloneElapsed").textContent = formatDuration(
      payload.standalone?.elapsed_seconds
    );
    $("standaloneRound").textContent = formatStandaloneRound(
      payload.standalone?.progress
    );
    return;
  }
  renderPerformance(payload.performance || {});
  renderRestart(payload.restart || {}, payload.mode || $("mode").value);
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
  const defaults = setupDefaultsForVersion($("rosVersion").value);
  $("rosSetup").placeholder = defaults.ros;
  $("cameraSetup").placeholder = defaults.cameraPlaceholder || defaults.camera;
  const domainEnabled = $("rosVersion").value === "2";
  $("rosDomainId").disabled = !domainEnabled;
  $("rosDomainId").title = domainEnabled
    ? "ROS 2 Domain ID，留空表示不设置"
    : "ROS 1 不使用 Domain ID";
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
  updateFormReadiness();
}

async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;
  state.setupDefaults = config.setup_defaults || {};
  $("rosVersion").value = config.ros_version || "2";
  $("rosDomainId").value = config.ros_domain_id || "";
  $("standaloneRosDomainId").value = config.ros_domain_id || "";
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
  $("workspacePath").title = config.auto_test_ws || "";
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
  if (state.workspace === "framework") {
    $("launchCount").textContent = `${files.length} 个 · ROS ${rosVersion}`;
  }
  updateLaunchConfigOptions(configured ? state.config.launch_config : "");
}

async function pollStatus() {
  try {
    const payload = await api(`/api/status?offset=${state.logOffset}`);
    setStatus(payload.status || "idle");
    if (payload.run_id) {
      $("runMeta").textContent = `${payload.run_id} ${payload.exit_code === null ? "" : `exit=${payload.exit_code}`}`;
      $("currentRunId").textContent = payload.run_id;
      $("currentMode").textContent =
        payload.runner_type === "standalone"
          ? state.standaloneTest?.title || "独立脚本"
          : payload.mode || "-";
    } else {
      $("runMeta").textContent = "";
      $("currentRunId").textContent = "未运行";
      $("currentMode").textContent = "—";
    }
    if (payload.command_lines) {
      renderCommands(payload.command_lines);
    }
    renderMonitor(payload);
    appendLogs(payload.logs || []);
    state.logOffset = payload.log_offset || state.logOffset;
    await updateResultSummary(payload);
    if (TERMINAL_RUN_STATUSES.has(payload.status)) {
      await loadRuns();
    }
  } catch (error) {
    appendLogs([`[UI] status poll failed: ${error.message}`]);
  }
}

async function startRun(event) {
  event.preventDefault();
  const validationErrors = validateFrameworkForm();
  if (!presentValidationErrors("runForm", "frameworkValidation", validationErrors)) {
    updateFormReadiness();
    return;
  }
  $("logOutput").textContent = "";
  $("reportView").textContent = "测试运行中...";
  state.logOffset = 0;
  state.resultSummaryRunId = null;
  $("runResultSummary").classList.add("is-hidden");
  try {
    const payload = await api("/api/run", {
      method: "POST",
      body: JSON.stringify(formPayload()),
    });
    setStatus(payload.status);
    renderCommands(payload.command_lines || []);
    renderMonitor(payload);
    appendLogs(payload.logs || []);
    state.logOffset = payload.log_offset || 0;
  } catch (error) {
    if (Array.isArray(error.payload?.errors)) {
      setStatus("idle");
      presentValidationErrors(
        "runForm",
        "frameworkValidation",
        serverValidationIssues(error)
      );
    } else {
      setStatus("failed");
      appendLogs([`[UI] start failed: ${error.message}`]);
    }
  }
}

async function startStandaloneRun(event) {
  event.preventDefault();
  const test = state.standaloneTest;
  if (!test) return;
  const validationErrors = validateStandaloneForm();
  if (!presentValidationErrors("standaloneForm", "standaloneValidation", validationErrors)) {
    updateFormReadiness();
    return;
  }
  let confirmedTestId = "";
  if (test.risk === "high") {
    if (!window.confirm(test.confirmation || "确认运行高风险脚本？")) return;
    confirmedTestId = test.id;
  }
  $("logOutput").textContent = "";
  $("reportView").textContent = "独立脚本运行中...";
  state.logOffset = 0;
  state.resultSummaryRunId = null;
  $("runResultSummary").classList.add("is-hidden");
  try {
    const payload = await api("/api/standalone/run", {
      method: "POST",
      body: JSON.stringify({
        test_id: test.id,
        confirmed_test_id: confirmedTestId,
        ros_domain_id: $("standaloneRosDomainId").value.trim(),
        values: standaloneCurrentValues(),
      }),
    });
    setStatus(payload.status);
    renderCommands(payload.command_lines || []);
    renderMonitor(payload);
    appendLogs(payload.logs || []);
    state.logOffset = payload.log_offset || 0;
  } catch (error) {
    if (Array.isArray(error.payload?.errors)) {
      setStatus("idle");
      presentValidationErrors(
        "standaloneForm",
        "standaloneValidation",
        serverValidationIssues(error, true)
      );
    } else {
      setStatus("failed");
      appendLogs([`[UI] standalone start failed: ${error.message}`]);
    }
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
    state.selectedRunIds.delete(runId);
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

function updateRunSelectionControls() {
  const checkboxes = [...document.querySelectorAll(".run-select")].filter(
    (checkbox) => !checkbox.disabled
  );
  const selectedCount = state.selectedRunIds.size;
  const selectAll = $("selectAllRuns");
  selectAll.checked = checkboxes.length > 0 && selectedCount === checkboxes.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < checkboxes.length;
  const deleteButton = $("deleteSelectedRuns");
  deleteButton.disabled = selectedCount === 0;
  deleteButton.textContent = selectedCount ? `删除所选 (${selectedCount})` : "删除所选";
}

function setAllRunsSelected(selected) {
  for (const checkbox of document.querySelectorAll(".run-select")) {
    if (checkbox.disabled) continue;
    checkbox.checked = selected;
    const runId = checkbox.dataset.runId;
    if (selected) {
      state.selectedRunIds.add(runId);
    } else {
      state.selectedRunIds.delete(runId);
    }
    checkbox.closest(".run-item").classList.toggle("batch-selected", selected);
  }
  updateRunSelectionControls();
}

async function deleteSelectedRuns() {
  const runIds = [...state.selectedRunIds];
  if (!runIds.length || !window.confirm(`删除选中的 ${runIds.length} 条历史记录？`)) {
    return;
  }

  const deleteButton = $("deleteSelectedRuns");
  deleteButton.disabled = true;
  deleteButton.textContent = "删除中...";
  try {
    const results = await Promise.allSettled(
      runIds.map((runId) =>
        api(`/api/runs/${encodeURIComponent(runId)}`, { method: "DELETE" })
      )
    );
    const deleted = [];
    const failed = [];
    results.forEach((result, index) => {
      const runId = runIds[index];
      if (result.status === "fulfilled") {
        deleted.push(runId);
        state.selectedRunIds.delete(runId);
      } else {
        failed.push(`${runId}: ${result.reason.message}`);
      }
    });

    if (deleted.includes(state.selectedRunId)) {
      state.selectedRunId = null;
      $("reportTitle").textContent = "";
      $("reportView").textContent = "选择一条历史记录查看结果。";
    }
    await loadRuns();
    if (failed.length) {
      appendLogs([`[UI] batch delete failed:\n${failed.join("\n")}`]);
    }
  } catch (error) {
    appendLogs([`[UI] batch delete failed: ${error.message}`]);
    updateRunSelectionControls();
  }
}

function runItem(run) {
  const item = document.createElement("div");
  item.className = `run-item${run.run_id === state.selectedRunId ? " selected" : ""}`;
  item.dataset.runId = run.run_id;

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "run-select";
  checkbox.dataset.runId = run.run_id;
  checkbox.setAttribute("aria-label", `选择任务 ${run.run_id}`);
  checkbox.disabled = ACTIVE_RUN_STATUSES.has(run.status);
  checkbox.checked = state.selectedRunIds.has(run.run_id);
  item.classList.toggle("batch-selected", checkbox.checked);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) {
      state.selectedRunIds.add(run.run_id);
    } else {
      state.selectedRunIds.delete(run.run_id);
    }
    item.classList.toggle("batch-selected", checkbox.checked);
    updateRunSelectionControls();
  });

  const title = document.createElement("div");
  title.className = "run-title";
  title.textContent = run.run_id;

  const subtitle = document.createElement("div");
  subtitle.className = "run-subtitle";
  const runnerLabel =
    run.runner_type === "standalone" ? `独立脚本 · ${run.test_id}` : run.mode || "unknown";
  subtitle.textContent = `${runnerLabel} | ${run.started_at || ""} | ${run.results_dir}`;

  const badge = document.createElement("span");
  badge.className = `badge ${run.status}`;
  badge.textContent = STATUS_LABELS[run.status] || run.status || "unknown";
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
  deleteButton.disabled = ACTIVE_RUN_STATUSES.has(run.status);
  if (deleteButton.disabled) {
    deleteButton.title = "运行中的任务不能删除";
  }
  deleteButton.addEventListener("click", () => deleteRun(run.run_id));

  actions.append(viewButton, deleteButton);
  item.append(checkbox, title, actions, subtitle);
  return item;
}

async function loadRuns() {
  const payload = await api("/api/runs");
  const list = $("runsList");
  list.innerHTML = "";
  const runs = payload.runs || [];
  const availableRunIds = new Set(
    runs
      .filter((run) => !ACTIVE_RUN_STATUSES.has(run.status))
      .map((run) => run.run_id)
  );
  state.selectedRunIds = new Set(
    [...state.selectedRunIds].filter((runId) => availableRunIds.has(runId))
  );
  for (const run of runs) {
    list.appendChild(runItem(run));
  }
  if (!list.children.length) {
    list.textContent = "暂无历史记录。";
  }
  updateRunSelectionControls();
}

function renderJsonSummary(results = {}) {
  const blocks = [];
  for (const [name, result] of Object.entries(results)) {
    if (result?.schema_version && result?.test_id) {
      const summaryLines = Object.entries(result.summary || {}).map(
        ([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`
      );
      const lines = [
        `test: ${result.test_id}`,
        `status: ${result.status || "unknown"}`,
        `duration: ${formatNumber(result.duration_seconds, 1)} s`,
        `started: ${result.started_at || ""}`,
        `ended: ${result.ended_at || ""}`,
        ...summaryLines,
      ];
      if (result.warnings?.length) lines.push(`warnings: ${JSON.stringify(result.warnings)}`);
      if (result.error) lines.push(`error: ${JSON.stringify(result.error)}`);
      blocks.push({ title: `${name} · ${result.test_id}`, text: lines.join("\n") });
      continue;
    }
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

  if (payload.events?.length) {
    const section = document.createElement("section");
    section.className = "summary-block";
    section.innerHTML = `<h3></h3><pre></pre>`;
    section.querySelector("h3").textContent = "events.jsonl";
    section.querySelector("pre").textContent = payload.events
      .map((event) => `${event.time || ""}  ${event.message || event.event || ""}`)
      .join("\n");
    view.appendChild(section);
  }

  if (!view.children.length) {
    view.textContent = "这条记录还没有可展示的报告。";
  }
}

async function init() {
  initTheme();
  setStatus("idle");
  const initialWorkspace =
    new URLSearchParams(window.location.search).get("workspace") === "standalone"
      ? "standalone"
      : "framework";
  switchWorkspace(initialWorkspace, false);
  $("runForm").addEventListener("submit", startRun);
  $("standaloneForm").addEventListener("submit", startStandaloneRun);
  $("runForm").addEventListener("input", () => {
    clearValidation("runForm", "frameworkValidation");
    updateFormReadiness();
  });
  $("standaloneForm").addEventListener("input", () => {
    clearValidation("standaloneForm", "standaloneValidation");
    updateFormReadiness();
  });
  $("stopButton").addEventListener("click", stopRun);
  $("standaloneStopButton").addEventListener("click", stopRun);
  $("showDevices").addEventListener("click", showDeviceInfo);
  $("refreshDevices").addEventListener("click", refreshDevices);
  $("refreshLaunches").addEventListener("click", loadLaunchFiles);
  $("refreshRuns").addEventListener("click", loadRuns);
  $("selectAllRuns").addEventListener("change", (event) => {
    setAllRunsSelected(event.target.checked);
  });
  $("deleteSelectedRuns").addEventListener("click", deleteSelectedRuns);
  $("copyLogs").addEventListener("click", copyLogs);
  $("clearLogs").addEventListener("click", () => {
    $("logOutput").textContent = "";
  });
  $("viewCurrentReport").addEventListener("click", async () => {
    const runId = state.currentSnapshot?.run_id;
    if (!runId) return;
    await loadRunDetail(runId);
    document.querySelector(".history-section")?.scrollIntoView({ behavior: "smooth" });
  });
  $("followLogs").addEventListener("change", () => {
    if ($("followLogs").checked) {
      $("logOutput").scrollTop = $("logOutput").scrollHeight;
    }
  });
  $("rosVersion").addEventListener("change", () => {
    const defaults = setupDefaultsForVersion($("rosVersion").value);
    $("rosSetup").value = defaults.ros;
    $("cameraSetup").value = defaults.camera;
    updateRosVersionControls();
    loadLaunchFiles();
  });
  $("mode").addEventListener("change", updateModeControls);
  $("launchFile").addEventListener("change", () => updateLaunchConfigOptions());
  $("launchConfig").addEventListener("change", updateSpecialConfigControls);
  $("standaloneTest").addEventListener("change", () =>
    renderStandaloneForm($("standaloneTest").value)
  );
  for (const button of document.querySelectorAll("[data-workspace]")) {
    button.addEventListener("click", () => switchWorkspace(button.dataset.workspace));
  }

  await loadConfig();
  await loadStandaloneTests();
  loadLaunchFiles();
  updateFormReadiness();
  await loadRuns();
  await pollStatus();
  state.polling = setInterval(pollStatus, 1000);
}

init().catch((error) => {
  setStatus("failed");
  appendLogs([`[UI] init failed: ${error.message}`]);
});

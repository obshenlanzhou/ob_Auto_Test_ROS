const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../orbbec_camera_auto_test_ui/static/app.js'), 'utf8');
function harness() {
  const timers = new Map();
  const logs = [];
  let serial = 0;
  const context = vm.createContext({
    DEFAULT_SETUPS: {'1': {ros: '/opt/ros/one/setup.bash'}, '2': {ros: '/opt/ros/humble/setup.bash'}},
    state: {setupDefaults: {'1': {ros_setup: '/opt/ros/one/setup.bash'}, '2': {ros_setup: '/opt/ros/jazzy/setup.bash'}}},
    document: {querySelectorAll: () => []},
    window: {
      setTimeout: callback => { timers.set(++serial, callback); return serial; },
      clearTimeout: id => timers.delete(id),
    },
    appendLogs: entries => logs.push(...entries),
    api: async () => { throw new Error('unexpected request'); },
  });
  vm.runInContext(source.slice(source.indexOf('function setupDefaultsForVersion('), source.indexOf('function launchFileForRosVersion(')), context);
  vm.runInContext(source.slice(source.indexOf('const packageJobs ='), source.indexOf('async function openRunDirectory(')), context);
  return {
    context, timers, logs,
    job: () => vm.runInContext('packageJobs.get("run")', context),
    async tick() {
      const [id, callback] = timers.entries().next().value;
      timers.delete(id);
      await callback();
    },
  };
}

test('each form restores custom ROS paths for the selected version', () => {
  const {context} = harness();
  for (const form of ['framework', 'standalone']) {
    const control = {value: `/custom/${form}/ros2/setup.bash`};
    context.initializeRosSetupVersions(control, '2');
    context.applyRosSetupDefault(control, context.setupDefaultsForVersion('1'), '1');
    assert.equal(control.value, '/opt/ros/one/setup.bash');
    control.value = `/custom/${form}/ros1/setup.bash`;
    context.applyRosSetupDefault(control, context.setupDefaultsForVersion('2'), '2');
    assert.equal(control.value, `/custom/${form}/ros2/setup.bash`);
    context.applyRosSetupDefault(control, context.setupDefaultsForVersion('1'), '1');
    assert.equal(control.value, `/custom/${form}/ros1/setup.bash`);
  }
});

test('page reload follows the existing package through completion without POST', async () => {
  const h = harness();
  h.context.api = async url => {
    assert.ok(url.endsWith('/package-progress'));
    return {package_id: 'one', status: 'completed', archive: '/run.tar.gz'};
  };
  h.context.restorePackageJob('run', {package_id: 'one', status: 'running', processed_bytes: 5, total_bytes: 10});
  assert.equal(h.job().active, true);
  // Repeated history refreshes must not start duplicate pollers.
  h.context.restorePackageJob('run', {package_id: 'one', status: 'running'});
  assert.equal(h.timers.size, 1);
  await h.tick();
  assert.equal(h.job().active, false);
  assert.equal(h.job().progress.status, 'completed');
  assert.equal(h.timers.size, 0);
  assert.match(h.logs.join(''), /run.tar.gz/);
});

test('409 attaches to existing work, then reports a real backend failure', async () => {
  const h = harness();
  h.context.api = async url => {
    if (url.endsWith('/package')) {
      const error = new Error('result packaging is already running');
      error.payload = {code: 'packaging_in_progress', progress: {status: 'running', package_id: 'one'}};
      throw error;
    }
    return {package_id: 'one', status: 'failed', error: 'disk full'};
  };
  await h.context.packageRun('run', {});
  assert.equal(h.job().active, true);
  assert.equal(h.logs.length, 0);
  await h.tick();
  assert.equal(h.job().progress.status, 'failed');
  assert.equal(h.job().active, false);
  assert.match(h.logs.join(''), /disk full/);
  assert.equal(h.timers.size, 0);
});

test('a previous completed archive cannot finish a newly submitted package', async () => {
  const h = harness();
  let completePost;
  h.context.api = url => url.endsWith('/package')
    ? new Promise(resolve => { completePost = resolve; })
    : Promise.resolve({package_id: 'old', status: 'completed', archive: '/old.tar.gz'});
  const request = h.context.packageRun('run', {});
  await h.tick();
  assert.equal(h.job().active, true);
  assert.equal(h.logs.length, 0);
  completePost({archive: '/new.tar.gz', progress: {package_id: 'new', processed_bytes: 10, total_bytes: 10}});
  await request;
  assert.equal(h.job().packageId, 'new');
  assert.equal(h.job().active, false);
  assert.match(h.logs.join(''), /new.tar.gz/);
  assert.equal(h.timers.size, 0);
});

test('restoring completed history does not poll or duplicate notifications', () => {
  const h = harness();
  h.context.restorePackageJob('run', {package_id: 'old', status: 'completed'});
  assert.equal(h.job().active, false);
  assert.equal(h.timers.size, 0);
  assert.equal(h.logs.length, 0);
});

test('a replaced package task and temporary network failure do not strand polling', async () => {
  const h = harness();
  h.context.restorePackageJob('run', {package_id: 'old', status: 'running'});
  h.context.api = async () => ({package_id: 'new', status: 'running'});
  await h.tick();
  assert.equal(h.job().packageId, 'new');
  h.context.api = async () => { throw new Error('network unavailable'); };
  await h.tick();
  assert.equal(h.job().active, true);
  assert.equal(h.timers.size, 1);
  h.context.api = async () => ({package_id: 'new', status: 'completed', archive: '/new.tar.gz'});
  await h.tick();
  assert.equal(h.job().active, false);
  assert.equal(h.timers.size, 0);
});

test('GPU detection ignores stale results even when returning to the same driver', async () => {
  const elements = {
    mode: {value: 'transport_latency'},
    launchFile: {value: 'gemini_330_series.launch.py'},
    rosVersion: {value: '2'},
    rosSetup: {value: '/opt/ros/jazzy/setup.bash'},
    cameraSetup: {value: '/driver-a/setup.bash'},
    transportGpu: {checked: true},
    transportGpuCapability: {},
  };
  const requests = [];
  const context = vm.createContext({
    $: id => elements[id],
    state: {},
    TRANSPORT_MODE: 'transport_latency',
    AbortController,
    api: async (url, options) => new Promise(resolve => requests.push({resolve, ...options})),
    updateSpecialConfigControls() {},
    updateFormReadiness() {},
    updateMonitorVisibility() {},
  });
  vm.runInContext(source.slice(
    source.indexOf('function isMultiCameraTransport('),
    source.indexOf('function updateSpecialConfigControls('),
  ), context);
  const first = context.refreshTransportLaunchArguments();
  assert.equal(elements.transportGpu.disabled, true);
  elements.cameraSetup.value = '/driver-b/setup.bash';
  const second = context.refreshTransportLaunchArguments();
  elements.cameraSetup.value = '/driver-a/setup.bash';
  const third = context.refreshTransportLaunchArguments();
  assert.equal(requests[0].body, requests[2].body);
  assert.equal(requests[0].signal.aborted, true);
  requests[2].resolve({arguments: [], gpu_backend: 'NITROS_BUFFER', gpu_problem: ''});
  await third;
  assert.equal(elements.transportGpu.disabled, false);
  requests[1].resolve({arguments: [], gpu_backend: 'CUDA_BUFFER', gpu_problem: ''});
  requests[0].resolve({arguments: [], gpu_backend: 'NONE', gpu_problem: 'No GPU backend'});
  await Promise.all([first, second]);
  assert.equal(elements.transportGpu.disabled, false);
  assert.match(elements.transportGpuCapability.textContent, /NITROS Buffer/);
  assert.equal(context.isGpuSelection(), true);
  assert.equal(context.transportCameraConfig(), '');
  assert.equal(JSON.parse(context.transportLaunchKey()).launch_file, 'gemini_330_series.launch.py');

  context.state.transportMultiCamera = true;
  context.state.transportCameraConfigPath = '/tmp/two-cameras.yaml';
  assert.equal(JSON.parse(context.transportLaunchKey()).transport_camera_config, '/tmp/two-cameras.yaml');
  context.state.transportCameraConfigPath = '';
  assert.equal(context.transportLaunchKey(), '');
  assert.match(context.gpuReceiverProblem(), /配置相机后/);
});

test('GPU transport table keeps stream details and reports only end-to-end latency', () => {
  const elements = new Map();
  const body = {
    rows: [],
    replaceChildren() { this.rows = []; },
    insertRow() {
      const cells = [];
      this.rows.push(cells);
      return {insertCell: () => {
        const cell = {};
        cells.push(cell);
        return cell;
      }};
    },
  };
  elements.set('fpsTableBody', body);
  const element = id => {
    if (!elements.has(id)) {
      elements.set(id, {classList: {toggle() {}, remove() {}}});
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    $: element,
    state: {},
    TERMINAL_RUN_STATUSES: new Set(['succeeded']),
    STATUS_LABELS: {succeeded: '完成'},
    formatNumber: (value, digits) => value.toFixed(digits),
  });
  vm.runInContext(
    source.slice(source.indexOf('function renderGpuPerformance('), source.indexOf('function renderPerformance(')),
    context,
  );
  const template = fs.readFileSync(path.join(__dirname, '../orbbec_camera_auto_test_ui/templates/index.html'), 'utf8');
  const header = template.match(/<tr id="gpuTransportColumns"[^>]*>([\s\S]*?)<\/tr>/)[1];
  assert.deepEqual([...header.matchAll(/<th\b[^>]*>([^<]*)<\/th>/g)].map(match => match[1]),
    ['相机', '流', '分辨率', '格式', '模式', '接收后端', 'FPS', '端到端平均时延 (ms)',
      '端到端 P95 时延 (ms)', '整机 CPU (%)', '整机 GPU (%)']);
  context.renderGpuPerformance({}, 'idle');
  assert.equal(body.rows[0][0].colSpan, 10);
  const result = {
    backend: 'CUDA_BUFFER',
    phase: 'cuda',
    duration_seconds: 60,
    latency_basis: 'frame_to_callback',
    final: true,
    rows: [{stream: 'depth', resolution: '640 x 480', stream_format: 'Y16',
      phase: 'cuda', backend: 'cuda', samples: 31, fps: 0.517,
      latency_ms: 35.499, latency_p95_ms: 40.153, longest_gap_sec: 58.999, cpu_fallback_frames: 0}],
    system: {cuda: {cpu_percent: 52.428, gpu_percent: 40.845}},
  };
  context.renderGpuPerformance(result, 'succeeded');
  assert.match(element('streamMetricsTitle').textContent, /帧时间戳到订阅回调/);
  assert.match(element('transportProgress').textContent, /测试时长 60 s/);
  assert.deepEqual(body.rows[0].map(cell => cell.textContent),
    ['depth', '640 x 480', 'Y16', 'CUDA Buffer', 'cuda', '0.517', '35.499', '40.153', '52.428', '40.845']);
  context.renderGpuPerformance({...result, configured: true,
    rows: [{...result.rows[0], camera_name: 'camera_02'}]}, 'succeeded');
  assert.equal(body.rows[0].length, 11);
  assert.equal(body.rows[0][0].textContent, 'camera_02');

  context.renderGpuPerformance({...result, latency_basis: undefined}, 'succeeded');
  assert.deepEqual(body.rows[0].slice(6, 8).map(cell => cell.textContent), ['n/a', 'n/a']);
  context.renderGpuPerformance({...result, final: false,
    rows: [{...result.rows[0], latency_p95_ms: undefined}]}, 'running');
  assert.deepEqual(body.rows[0].slice(6, 8).map(cell => cell.textContent), ['35.499', '—']);
});

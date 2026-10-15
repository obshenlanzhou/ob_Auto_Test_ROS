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

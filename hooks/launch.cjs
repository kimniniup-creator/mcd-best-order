// Bootstrap with platform-native executables; never inspect a project's .venv.
const {spawn, spawnSync} = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');

const mode = process.argv[2] || process.argv[1];
const env = {...process.env, PYTHONUTF8: '1'};
for (const key of ['VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH', 'UV_PROJECT_ENVIRONMENT', 'UV_PROJECT', 'UV_WORKING_DIR', 'UV_CONFIG_FILE']) delete env[key];
const install = '缺少 uv。Windows PowerShell：powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"；macOS/Linux：curl -LsSf https://astral.sh/uv/install.sh | sh。安装后重开终端和你的 Agent。';
function context(message) {
  console.log(JSON.stringify({hookSpecificOutput: {hookEventName: 'SessionStart', additionalContext: message}}));
}
function unavailable(message) {
  console.error(message);
  if (mode !== 'server') { context(message); return; }
  readline.createInterface({input: process.stdin}).on('line', line => {
    let request;
    try { request = JSON.parse(line); } catch { return; }
    if (!Object.hasOwn(request, 'id')) return;
    let result = {};
    if (request.method === 'initialize') result = {protocolVersion: request.params?.protocolVersion || '2025-03-26', capabilities: {tools: {}}, serverInfo: {name: '点餐安装提示', version: '0.9.3'}, instructions: message};
    if (request.method === 'tools/list') result = {tools: [{name: 'mcd_start', description: message, inputSchema: {type: 'object', properties: {utterance: {type: 'string'}}, required: ['utterance']}, annotations: {readOnlyHint: true, destructiveHint: false, openWorldHint: false}}]};
    if (request.method === 'tools/call') result = {content: [{type: 'text', text: message}], structuredContent: {stage: 'install', next_tool: null, display_markdown: message}, isError: false};
    console.log(JSON.stringify({jsonrpc: '2.0', id: request.id, result}));
  });
}
if (!['server', 'hook', 'check', 'probe'].includes(mode)) process.exit(2);
const found = spawnSync('uv', ['python', 'find', '--offline', '--no-config', '--system', '--managed-python', '3.14'], {env, encoding: 'utf8', windowsHide: true});
if (found.error || found.status !== 0) {
  unavailable(found.error?.code === 'ENOENT' ? install : '未找到可用的 uv 托管 Python 3.14。请运行 uv python install 3.14，然后重开 Agent；运行 coupon-split doctor 查看启动依赖。\n' + (found.stderr || found.error?.message || ''));
  if (mode === 'probe') process.exitCode = 1;
} else {
  const python = found.stdout.trim();
  const args = mode === 'probe' ? ['-I', '-c', 'import json,sys; print(json.dumps({"executable":sys.executable,"prefix":sys.prefix,"base_prefix":sys.base_prefix}))'] : ['-I', path.join(__dirname, mode === 'server' ? 'stdio_gateway.py' : 'order_context.py'), ...(mode === 'check' ? ['startup'] : [])];
  const child = spawn(python, args, {env, stdio: 'inherit', windowsHide: true});
  child.on('error', error => { unavailable('Python 启动失败：' + error.message + '。请运行 coupon-split doctor 检查；若系统拦截，请修复 uv 托管 Python 安装。'); process.exitCode = 1; });
  child.on('exit', (code, signal) => { process.exitCode = code ?? 1; });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
}

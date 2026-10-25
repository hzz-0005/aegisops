const state = {
  products: [], selected: null, pendingKey: null, overview: null,
  incidentId: new URLSearchParams(location.search).get('incident'),
  handledRun: null, detailLoaded: false, detailRequestId: 0, overviewBusy: false,
};
const $ = (id) => document.getElementById(id);
const money = (cents) => `¥${(cents / 100).toFixed(2)}`;
const date = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';

function node(tag, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'X-AegisOps-Console': 'lab-console-v1', 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`);
  return data;
}

function showView(name) {
  $('shop-view').classList.toggle('hidden', name !== 'shop');
  $('ops-view').classList.toggle('hidden', name !== 'ops');
  document.querySelectorAll('.nav-button').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
  const url = new URL(location.href);
  if (name === 'ops') {
    url.searchParams.set('view', 'ops');
    if (state.incidentId) url.searchParams.set('incident', state.incidentId);
  }
  else url.searchParams.delete('view');
  if (name !== 'ops') url.searchParams.delete('incident');
  history.replaceState(null, '', url);
  if (name === 'ops' && !state.overview) refreshOverview();
}

function quantity() {
  const input = $('quantity');
  const value = Math.max(1, Math.min(100, Number.parseInt(input.value, 10) || 1));
  input.value = String(value);
  return value;
}

function updateCheckout() {
  const product = state.selected;
  $('selected-product').textContent = product ? `${product.name} · ${product.sku}` : '请先选择商品';
  $('order-total').textContent = product ? money(product.unit_price_cents * quantity()) : '¥0.00';
  $('checkout-button').disabled = !product;
}

function renderProducts() {
  const target = $('catalog');
  target.replaceChildren();
  $('catalog-count').textContent = `${state.products.length} 件商品`;
  if (!state.products.length) { target.append(node('p', 'detail-empty', '商品服务暂不可用，请稍后刷新。')); return; }
  state.products.forEach((product, index) => {
    const card = node('button', `product-card${state.selected?.sku === product.sku ? ' selected' : ''}`);
    card.type = 'button';
    card.setAttribute('aria-pressed', String(state.selected?.sku === product.sku));
    const art = node('div', `product-art tone-${index % 4}`);
    art.append(node('span', '', product.category));
    const copy = node('div', 'product-copy');
    copy.append(node('div', 'product-category', product.sku), node('div', 'product-name', product.name), node('div', 'product-description', product.description));
    const foot = node('div', 'product-foot');
    foot.append(node('span', 'price', money(product.unit_price_cents)), node('span', 'stock', `库存 ${product.available}`));
    copy.append(foot);
    card.append(art, copy);
    card.addEventListener('click', () => { state.selected = product; state.pendingKey = null; $('checkout-result').textContent = ''; renderProducts(); updateCheckout(); });
    target.append(card);
  });
}

async function loadCatalog() {
  try { state.products = await request('/api/catalog'); renderProducts(); }
  catch (error) { $('catalog').replaceChildren(node('p', 'detail-empty', `无法加载商品：${error.message}`)); }
}

async function checkout() {
  if (!state.selected) return;
  if (!state.pendingKey) state.pendingKey = crypto.randomUUID();
  const button = $('checkout-button');
  button.disabled = true;
  button.textContent = '正在提交…';
  $('checkout-result').className = 'result-panel';
  $('checkout-result').textContent = '正在等待订单与付款服务响应。';
  try {
    const result = await request('/api/checkout', { method: 'POST', body: JSON.stringify({ idempotency_key: state.pendingKey, sku: state.selected.sku, quantity: quantity() }) });
    $('checkout-result').textContent = `订单 ${result.status} · ${money(result.amount_cents)}\n订单号：${result.order_id}\n付款号：${result.payment_id || '等待中'}`;
    state.pendingKey = null;
  } catch (error) {
    $('checkout-result').className = 'result-panel error';
    $('checkout-result').textContent = `结账未完成：${error.message}\n再次点击将复用同一订单键。`;
  } finally { button.disabled = false; button.textContent = '提交模拟订单'; }
}

function renderScenarios() {
  const target = $('scenarios');
  target.replaceChildren();
  const active = state.overview.runs.some((run) => run.status === 'RUNNING');
  state.overview.scenarios.forEach((scenario) => {
    const item = node('div', 'scenario-item');
    const copy = node('div');
    copy.append(node('span', '', scenario.service), node('strong', '', scenario.title), node('p', '', scenario.description));
    const button = node('button', 'scenario-button', '启动故障');
    button.type = 'button'; button.disabled = active;
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await request(`/api/scenarios/${scenario.id}/run`, { method: 'POST' }); await refreshOverview(); }
      catch (error) { $('run-output').textContent = `启动失败：${error.message}`; button.disabled = false; }
    });
    item.append(copy, button); target.append(item);
  });
}

function renderIncidents() {
  const target = $('incidents');
  target.replaceChildren();
  const incidents = state.overview.incidents;
  const filter = $('incident-filter').value.trim().toLocaleLowerCase();
  const visible = incidents.filter((incident) => incident.title.toLocaleLowerCase().includes(filter)).slice(0, 20);
  $('incident-count').textContent = `${visible.length} / ${incidents.length}`;
  if (!incidents.length) { target.append(node('p', 'detail-empty', '运行一个故障场景后，事故会出现在这里。')); return; }
  if (!visible.length) { target.append(node('p', 'detail-empty', '没有匹配的事故，请换一个关键词。')); return; }
  visible.forEach((incident) => {
    const button = node('button', `incident-row${state.incidentId === incident.id ? ' selected' : ''}`);
    button.type = 'button';
    button.setAttribute('aria-current', String(state.incidentId === incident.id));
    const copy = node('span');
    copy.append(node('strong', '', incidentTitle(incident.title)), node('small', '', `${date(incident.opened_at)} · ${incident.alert_count} 条告警`));
    button.append(copy, node('span', `status-pill ${incident.status === 'OPEN' ? 'open' : ''}`, incident.status === 'OPEN' ? '未结' : incident.status === 'RESOLVED' ? '已结' : incident.status));
    button.addEventListener('click', () => {
      selectIncident(incident.id);
      if (matchMedia('(max-width: 760px)').matches) $('incident-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    target.append(button);
  });
}

function renderRun() {
  const run = state.overview.runs[0];
  $('run-status').textContent = run ? `${run.title} · ${run.status}` : '尚未运行';
  $('run-output').textContent = run ? (run.output || '场景正在运行；脚本结束后会显示执行结果。') : '启动一个场景后，执行结果会显示在这里。';
}

async function refreshOverview() {
  if (state.overviewBusy) return;
  state.overviewBusy = true;
  try {
    state.overview = await request('/api/overview');
    $('health-dot').className = `dot ${state.overview.api_ready ? 'ok' : 'bad'}`;
    $('health-text').textContent = state.overview.api_ready ? '控制面在线' : '控制面不可用';
    $('last-refresh').textContent = `${new Date().toLocaleTimeString('zh-CN', { hour12: false })} 已同步`;
    $('stat-open').textContent = state.overview.incidents.filter((item) => item.status === 'OPEN').length;
    $('stat-investigating').textContent = state.overview.incidents.filter((item) => item.processing_phase === 'INVESTIGATING').length;
    $('stat-resolved').textContent = state.overview.incidents.filter((item) => item.status === 'RESOLVED').length;
    renderDetector();
    renderRequestSignals();
    renderScenarios(); renderRun(); renderIncidents();
    const latest = state.overview.runs[0];
    if (latest?.status === 'SUCCEEDED' && state.handledRun !== latest.id) {
      state.handledRun = latest.id;
      const created = state.overview.incidents.find((incident) =>
        new Date(incident.opened_at).getTime() >= new Date(latest.started_at).getTime() - 30000
      );
      if (created) selectIncident(created.id);
    }
    if (!state.detailLoaded && state.overview.incidents.length) {
      const chosen = state.overview.incidents.find((item) => item.id === state.incidentId) || state.overview.incidents[0];
      selectIncident(chosen.id);
    }
  } catch (error) {
    $('health-dot').className = 'dot bad';
    $('health-text').textContent = '连接失败';
    $('last-refresh').textContent = '同步失败';
    $('run-output').textContent = `无法获取现场状态：${error.message}`;
  } finally {
    state.overviewBusy = false;
  }
}

function renderDetector() {
  const runs = state.overview.detector_runs;
  if (!runs) {
    $('detector-summary').textContent = '监测状态暂不可用';
    $('detector-detail').textContent = '控制面仍可查看事故；请检查独立监测进程。';
    return;
  }
  const blocked = runs.filter((run) => run.status === 'BLOCKED');
  const retrying = runs.filter((run) => run.status === 'RETRYABLE');
  const finalized = runs.filter((run) => run.status === 'FINALIZED');
  const pending = runs.filter((run) => run.status === 'PENDING');
  const running = runs.filter((run) => run.status === 'RUNNING');
  const labels = { PENDING: '待处理', RUNNING: '处理中', RETRYABLE: '等待重试', BLOCKED: '已阻塞', FINALIZED: '已定稿' };
  $('detector-summary').textContent = `最近 ${runs.length} 窗：定稿 ${finalized.length} · 待处理 ${pending.length} · 处理中 ${running.length} · 重试 ${retrying.length} · 阻塞 ${blocked.length}`;
  const latest = runs[0];
  $('detector-detail').textContent = latest
    ? `最近窗口 ${date(latest.window_start)} · ${labels[latest.status] || '未知状态'} · 仅记录请求失败症状，不判断根因或执行修复。`
    : '等待第一个已过水位的窗口；监测结果不会自动生成事故。';
  const late = state.overview.late_detector_runs;
  if (late === null) {
    $('detector-detail').textContent += ' 迟到补算状态暂不可用。';
  } else if (late?.length) {
    const count = (status) => late.filter((run) => run.status === status).length;
    $('detector-detail').textContent += ` 迟到补算最近 ${late.length} 批：完成 ${count('FINALIZED')}，重试 ${count('RETRYABLE')}，阻塞 ${count('BLOCKED')}；原窗口保持不变。`;
    const incomplete = late.filter((run) => run.snapshot_complete === false).length;
    if (incomplete) $('detector-detail').textContent += ` 其中 ${incomplete} 批超过处理上限，已阻塞；已记录成员数仅为下限。`;
  }
}

function appendBlock(parent, title, content) {
  const block = node('div', 'detail-block');
  block.append(node('h3', '', title), content); parent.append(block);
}

function renderRequestSignals() {
  const container = $('request-signal-list');
  const signals = (state.overview.request_signals || []).slice(0, 6);
  const signature = JSON.stringify(signals.map((signal) => signal.id));
  if (container.dataset.signalIds === signature) return;
  container.dataset.signalIds = signature;
  container.replaceChildren();
  signals.forEach((signal) => {
    const form = node('form', 'request-evidence-form');
    const mode = signal.detection_mode === 'LATE_COMPENSATION' ? '迟到事件补算' : '原始窗口';
    form.append(node('p', 'small-muted', `${mode} · ${signal.service} · ${signal.failure_code} · ${signal.event_count} 条 · ${new Date(signal.window_start).toLocaleString('zh-CN')}`));
    const label = node('label', '', '调查理由');
    const input = document.createElement('input');
    input.required = true; input.minLength = 3; input.maxLength = 300;
    input.placeholder = '说明为什么需要调查这些请求失败';
    label.append(input); form.append(label);
    const button = node('button', 'secondary-button', '送入调查');
    button.type = 'submit'; form.append(button);
    const result = node('p', 'small-muted'); result.setAttribute('role', 'status');
    form.append(result);
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); button.disabled = true;
      try {
        await request(`/api/request-signals/${signal.id}/promote`, {
          method: 'POST', body: JSON.stringify({reason: input.value.trim()}),
        });
        result.textContent = '已送入调查，稍后在事故队列查看。重复提交不会重复创建告警。';
        await refreshOverview();
      } catch (error) { result.textContent = error.message; }
      finally { button.disabled = false; }
    });
    container.append(form);
  });
}

function evidenceLabel(item) {
  if (item.kind === 'REQUEST_SIGNAL_MEMBERSHIP') {
    const p = item.payload;
    return `${item.id.slice(0, 8)} · 已核验告警来源成员 · ${p.service} · ${p.event_count} 条请求事实 · 直接来源绑定 · 根因未确定，当前故障状态未知。`;
  }
  const facts = item.payload || {};
  let suffix = '';
  if (item.kind === 'HTTP_REQUEST_ACTIVITY') {
    suffix = ` · 请求增量峰值 ${facts.request_peak_increase ?? '未知'}，5xx ${facts.error_peak_increase ?? '无指标序列'}`;
  } else if (item.kind === 'SERVICE_DEPENDENCIES') {
    suffix = ` · 依赖 ${facts.dependencies?.join('、') || '无登记项'}`;
  } else if (item.kind === 'RUNTIME_STATE_OBSERVATION') {
    const names = {running: '运行中', paused: '暂停', exited: '退出', restarting: '重启中', created: '已创建', dead: '失效', removing: '移除中'};
    return `${item.id.slice(0, 8)} · 运行时状态观测 · ${facts.service || '未知服务'} · ${names[facts.state] || '未知状态'} · 历史状态，不能单独证明全部根因。`;
  } else if (item.kind === 'VERIFIED_REQUEST_FAILURE_LINK') {
    return `${item.id.slice(0, 8)} · 已核验请求上下游关系 · ${facts.upstream_service || '未知服务'} → ${facts.downstream_service || '未知服务'} · 路径 ${facts.span_path?.length || 0} 个Span · Trace ${facts.trace_id} · 未绑定聚合告警，根因未确定。`;
  } else if (item.kind === 'VERIFIED_DEPENDENCY_REQUEST') {
    const names = {dependency_timeout: '依赖响应超时', dependency_transport: '依赖连接或传输失败', dependency_http_5xx: '依赖HTTP错误'};
    return `${item.id.slice(0, 8)} · 已核验依赖请求 · ${facts.service || '未知服务'} → ${facts.dependency || '未知依赖'} · ${names[facts.failure_code] || '未知失败码'} · Trace ${facts.trace_id} · 未绑定聚合告警。`;
  } else if (item.kind === 'VERIFIED_DATABASE_OPERATION') {
    const names = {database_lock_timeout: '锁等待超时', database_query_cancelled: '查询被取消', database_error: '数据库操作失败'};
    const operations = {'inventory.lookup': '库存查询', 'catalog.list': '商品目录读取'};
    const callers = [...new Set((facts.caller_links || []).map(link => link.upstream_service))];
    const path = callers.length ? ` · 已核验调用路径：${callers.join('、')} → ${facts.service}` : '';
    return `${item.id.slice(0, 8)} · 已核验数据库操作 · ${facts.service || '未知服务'} · ${operations[facts.operation] || facts.operation} · ${names[facts.failure_code] || '未知失败码'} · SQLSTATE ${facts.sqlstate} · ${facts.duration_ms} 毫秒${path} · 未绑定聚合告警，当前状态未知。`;
  } else if (item.kind === 'DEPENDENCY_PROBE_ACTIVITY') {
    const reasonNames = {timeout: '响应超时', transport: '连接或传输失败', http_status: 'HTTP非成功状态'};
    const reasons = Object.entries(facts.failure_reason_counts || {}).filter(([key, value]) => reasonNames[key] && value > 0).map(([key, value]) => `${reasonNames[key]} ${value} 个采样`).join('、');
    return `${item.id.slice(0, 8)} · 端点探针上下文 · ${facts.service || '未知服务'} · ${facts.sample_count ?? 0} 个采样中 ${facts.failure_sample_count ?? 0} 个不可达${reasons ? `（${reasons}）` : ''}；不能单独证明根因。`;
  }
  return `${item.id.slice(0, 8)} · ${item.source_type} / ${item.kind} / ${item.polarity} · ${item.subject}${suffix}`;
}

const rootLabels = {
  redis_unavailable: '支付缓存 Redis 不可用',
  order_db_pool_exhaustion: '订单服务数据库连接池耗尽',
  bad_order_deployment: '订单服务错误部署',
  inventory_unavailable: '库存依赖不可用',
  gateway_order_failures: '网关观测到订单服务失败',
};

const incidentTitles = {
  'A faulty order deployment is rejecting checkouts': '订单服务错误部署导致结账失败',
  'Payment requests are failing because Redis is unavailable': 'Redis 不可用导致支付请求失败',
  'Gateway checkouts are failing at the order service': '订单服务故障导致网关结账失败',
};

const statusLabels = {
  OPEN: '未结案', RESOLVED: '已结案', CANCELLED: '已取消',
  INVESTIGATING: '调查中', ESCALATED: '已升级',
  SUPPORTED: '证据支持', INCONCLUSIVE: '证据不足',
  RUNNING: '运行中', COMPLETED: '已完成',
  WAITING_OPERATOR: '等待人工', REPORT_READY: '报告已生成',
  PROPOSED: '已提议', AUTHORIZED: '已授权', REQUESTED: '待审批',
  APPROVED: '已批准', REJECTED: '已拒绝', DENIED: '已拒绝',
  PRECONDITION_FAILED: '前置条件不满足', SUCCEEDED: '已成功',
};

function incidentTitle(value) { return incidentTitles[value] || value; }
function statusText(value) {
  return String(value || '未知').split(':').map((part) => statusLabels[part] || part).join(' · ');
}

function investigationSummary(summary, root) {
  if (summary === 'Insufficient or conflicting evidence; operator investigation required.') {
    return '现有证据不足或存在冲突，需要运维人员继续调查。';
  }
  if (summary?.startsWith('Likely root cause: ') && rootLabels[root]) {
    return `可能根因：${rootLabels[root]}；执行任何操作前请核对关联证据。`;
  }
  return summary;
}

function planCopy(plan) {
  if (plan.catalog_version !== 'remediation-catalog-v1') return plan;
  const actionType = plan.actions?.[0]?.snapshot?.action_type;
  const legacy = {
    restart_cache: ['先确认 Redis 进程不可用，再重启支付缓存。', '支付缓存连接和正在处理的结账请求。'],
    restart_service: ['先核实连接池确已耗尽；重启订单服务可能释放被占用的连接。', '重启期间的订单结账请求。'],
    rollback_service: ['运维人员核对发布指纹和影响范围后，回退订单服务到已记录的上一版本。', '订单结账请求和订单服务的发布状态。'],
  }[actionType];
  if (!legacy) return plan;
  return { ...plan, rationale: legacy[0], blast_radius: legacy[1], risk_summary: '高风险：执行前必须经过独立人工审批。' };
}

function reviewSummary(review) {
  const text = review?.summary;
  if (text?.startsWith('Incident evidence supports the bad_order_deployment hypothesis:')
      && text.endsWith('deterministic conclusion is unchanged.')) {
    return '现有证据支持订单服务错误部署假设：发布变更伴随订单服务与网关告警、错误追踪和指标增长；未报告冲突证据。确定性调查结论保持不变。';
  }
  return text;
}

function limitationText(value) {
  const unknown = /^no signal definition for (.+) on (.+)$/.exec(value);
  if (unknown) return `服务 ${unknown[2]} 的告警 ${unknown[1]} 缺少可信信号定义`;
  const noHttp = /^no generic HTTP samples for (.+)$/.exec(value);
  if (noHttp) return `服务 ${noHttp[1]} 没有可用的通用 HTTP 指标样本`;
  return value;
}

function renderDetail(data) {
  const target = $('incident-detail');
  target.className = '';
  target.replaceChildren();
  const report = data.investigation;
  $('detail-meta').textContent = `${data.incident.severity} · ${statusText(data.incident.processing_phase)} · v${data.incident.version}`;
  const intro = node('div', 'case-intro');
  const title = node('div');
  title.append(node('h3', '', incidentTitle(data.incident.title)), node('p', '', `事故 ID ${data.incident.id} · ${statusText(data.incident.status)}`));
  intro.append(title, node('span', `status-pill ${data.incident.status === 'OPEN' ? 'open' : ''}`, data.incident.severity));
  target.append(intro);
  const flow = node('div', 'flow-strip');
  const stages = [
    ['告警归组', '事故已创建', true],
    ['只读调查', report ? statusText(report.status) : '等待报告', !!report],
    ['模型复核', data.model_review ? statusText(data.model_review.status) : '可选', data.model_review?.status === 'COMPLETED'],
    ['修复治理', data.remediation_plan ? statusText(data.remediation_plan.status) : '未提议', !!data.remediation_plan],
  ];
  stages.forEach(([label, value, done]) => {
    const stage = node('div', `flow-stage ${done ? 'done' : 'pending'}`);
    stage.append(node('span', '', label), node('strong', '', value));
    flow.append(stage);
  });
  target.append(flow);
  const columns = node('div', 'detail-columns');
  const left = node('div');
  appendBlock(left, '调查结论', node('p', '', report ? `${investigationSummary(report.summary, report.primary_hypothesis)} 根因：${rootLabels[report.primary_hypothesis] || '尚未确定'}；证据分数：${Math.round(report.confidence * 100)}%。` : '调查报告尚未生成。请等待工作流并刷新。'));
  if (report) {
    if (report.agent_trace) {
      const trace = report.agent_trace;
      const area = node('div');
      const verificationNames = {OBSERVATION_VERIFIED: '历史观测通过核验', CONFLICTED: '存在冲突，需复核', INSUFFICIENT: '证据不足', UNSUPPORTED_CLAIM: '尚无可核验主张'};
      const stops = {MODEL_FINISHED: '模型结束调查', MODEL_BUDGET: '模型轮次已用尽', TOOL_BUDGET: '工具次数已用尽', TIME_BUDGET: '总时间已用尽', REPEATED_QUERY: '阻止重复查询', VERSION_CHANGED: '事故版本或成员已变化', MODEL_UNAVAILABLE: '模型不可用', INVALID_MODEL_DECISION: '模型输出未通过校验', INTERRUPTED_UNKNOWN_RESULT: '活动中断，调用结果不明', NO_ALERTS: '没有可调查的告警'};
      area.append(node('p', '', `${trace.model_calls} 次模型 · ${trace.tool_calls} 次只读取证 · ${stops[trace.stop_reason] || trace.stop_reason}`));
      if (trace.stop_reason === 'FINAL_SUMMARY_REQUIRED') area.append(node('p', 'small-muted', '最后一轮未按要求总结，后台已停止追加工具。'));
      trace.steps.filter(step => step.phase === 'TOOL').forEach(step => {
        const details = node('details');
        const statuses = {COMPLETED: '已取得观测', NO_DATA: '没有可用样本', FAILED: '工具失败', RESERVED: '结果不明'};
        details.append(node('summary', '', `${step.decision.tool.name} · ${step.decision.tool.service} · ${statuses[step.status] || step.status}`));
        details.append(node('p', '', `取证理由：${step.decision.reason}`));
        const facts = node('ul');
        step.evidence_ids.forEach(id => {
          const item = report.evidence.find(e => e.id === id);
          facts.append(node('li', '', item ? evidenceLabel(item) : `证据 ${id}`));
        });
        details.append(facts, node('p', 'small-muted', `耗时 ${step.elapsed_ms} 毫秒${step.error_code ? ` · ${step.error_code}` : ''}`));
        area.append(details);
      });
      trace.hypotheses.forEach(h => area.append(node('p', '', `${h.label}：${h.rationale}（${h.state === 'REJECTED' ? '已排除候选' : h.state === 'WEAKENED' ? '支持减弱' : '待验证候选'}）`)));
      (report.candidate_verifications || []).forEach(check => {
        const coverage = check.source_member_count === null ? '告警来源未绑定' : `直接来源覆盖 ${check.verified_member_count}/${check.source_member_count}`;
        area.append(node('p', 'small-muted', `候选 ${check.candidate_index}：${report.is_stale ? '报告已陈旧，需重新调查' : verificationNames[check.status]} · ${coverage} · 根因未确定，当前状态未知`));
      });
      const gates = report.root_cause_gates || [];
      const gateBlockers = { NO_VERIFIED_CLAIM: '没有可核验的历史观测主张', MECHANISM_UNPROVEN: '机制事实未核验', CONFLICTED_OBSERVATIONS: '观测存在冲突', SOURCE_COVERAGE_PARTIAL: '来源成员未全部核验', SOURCE_SCOPE_UNBOUND: '告警没有直接来源绑定', CURRENT_FAULT_STATUS_UNKNOWN: '当前故障状态未知', SNAPSHOT_CHANGED: '事故已变化' };
      const scopeNames = { FULL_DIRECT_SOURCE: '来源全覆盖', PARTIAL_DIRECT_SOURCE: '来源部分覆盖', UNBOUND: '告警来源未绑定' };
      gates.forEach(gate => {
        const passed = gate.decision === 'MECHANISM_SCOPE_SUPPORTED';
        const blockers = (gate.blocking_reasons || []).map(code => gateBlockers[code] || code).join('、');
        area.append(node('p', 'small-muted', `候选 ${gate.candidate_index} 根因门禁：${passed ? '机制证明与来源覆盖已满足' : '门禁未通过'} · ${scopeNames[gate.source_scope_status] || gate.source_scope_status}${blockers ? ` · 缺口：${blockers}` : ''} · 当前不可映射修复动作`));
      });
      appendBlock(left, '自主调查过程', area);
    }
    const evidence = node('ul');
    report.evidence.slice(0, 12).forEach((item) => evidence.append(node('li', '', evidenceLabel(item))));
    appendBlock(left, `证据（${report.evidence.length}）`, evidence);
    if (report.limitations?.length) appendBlock(left, '调查限制', node('p', '', report.limitations.map(limitationText).join('；')));
  }
  const candidates = data.request_candidates;
  const candidateArea = node('div');
  const candidateStatuses = { NO_DIRECT_SOURCE: '没有直接来源锚点', OBSERVED: '核验完成', INCOMPLETE: '核验不完整', STALE: '结果已过期' };
  const candidateConflicts = { SOURCE_MEMBERS_UNCOVERED: '本来源成员未完全覆盖', OTHER_MEMBERS_UNCOVERED: '对端成员未完全覆盖', SINGLE_TRACE_ONLY: '仅一条追踪', MULTIPLE_PEER_SOURCE_GROUPS: '同方向存在多个来源组', INCOMPLETE_OBSERVATION: '核验不完整', STALE_OBSERVATION: '核验已过期' };
  const failureNames = { upstream_http_5xx: '上游 HTTP 5xx', upstream_transport: '上游传输失败', dependency_http_5xx: '依赖 HTTP 5xx', dependency_transport: '依赖传输失败', dependency_timeout: '依赖响应超时', database_lock_timeout: '数据库锁等待超时', database_query_cancelled: '查询被取消', database_error: '数据库操作失败' };
  candidateArea.append(node('p', '', candidates ? `${candidateStatuses[candidates.status] || candidates.status} · 来源 ${candidates.inspected_source_alert_count}/${candidates.source_alert_count} · 追踪核验 ${candidates.verified_trace_count}/${candidates.selected_trace_count}` : '候选关系暂不可读取，请刷新。'));
  if (candidates) {
    candidateArea.append(node('p', 'small-muted', candidates.interpretation));
    candidates.candidates.forEach((item) => {
      const details = node('details');
      details.append(node('summary', '', `${item.direction === 'DOWNSTREAM' ? '下游' : '上游'}来源 · ${item.distinct_trace_count} 条已核验追踪 · 覆盖 ${item.source_covered_event_ids.length}/${item.source_member_count} → ${item.other_covered_event_ids.length}/${item.other_member_count}`));
      details.append(node('p', '', `${item.other_service} · ${failureNames[item.other_failure_code] || item.other_failure_code}`));
      const link = node('a', '', `查看事故 ${item.other_incident_id.slice(0, 8)} · v${item.other_incident_version}`);
      link.href = `/?view=ops&incident=${encodeURIComponent(item.other_incident_id)}`;
      link.addEventListener('click', (event) => { event.preventDefault(); selectIncident(item.other_incident_id); });
      details.append(link, node('p', '', '支持：两端持久化直接来源成员 + 已核验父 Span 调用路径。'));
      details.append(node('p', '', `冲突与限制：${item.conflicting_features.map((key) => candidateConflicts[key] || key).join('；') || '本候选没有覆盖冲突，仍需人工核查根因。'}`));
      const sourceIds = node('p', 'small-muted', `来源告警 ${item.source_alert_id.slice(0, 8)} → ${item.other_alert_id.slice(0, 8)}；根因未确定，当前状态未知。`);
      sourceIds.title = `来源告警 ${item.source_alert_id} → ${item.other_alert_id}`;
      details.append(sourceIds);
      const proofDetails = node('details');
      proofDetails.append(node('summary', '', `查看 ${item.proofs.length} 对请求路径明细`));
      const proofs = node('ul');
      item.proofs.forEach((proof) => {
        const entry = node('li', '', `${proof.upstream_service} → ${proof.downstream_service} · Trace ${proof.trace_id.slice(0, 12)} · 事件 ${proof.upstream_event_id.slice(0, 8)} → ${proof.downstream_event_id.slice(0, 8)}`);
        entry.title = `Trace ${proof.trace_id} · 事件 ${proof.upstream_event_id} → ${proof.downstream_event_id} · Span ${proof.span_path.join(' → ')}`;
        proofs.append(entry);
      });
      proofDetails.append(proofs); details.append(proofDetails); candidateArea.append(details);
    });
    if (candidates.limitations.length) candidateArea.append(node('p', '', candidates.limitations.join('；')));
  }
  appendBlock(left, '来源绑定的请求关系 · 人工核查候选', candidateArea);
  if (data.trace_overlaps?.length) {
    const details = node('details');
    details.append(node('summary', '', `${data.trace_overlaps.length} 份报告有追踪交集 · 未证明事故相关`));
    details.append(node('p', '', data.trace_overlaps[0].interpretation));
    const overlaps = node('ul');
    data.trace_overlaps.forEach((item) => {
      const entry = node('li');
      const link = node('a', '', `查看其他报告 ${item.other_incident_id.slice(0, 8)}`);
      link.href = `/?view=ops&incident=${encodeURIComponent(item.other_incident_id)}`;
      link.addEventListener('click', (event) => { event.preventDefault(); selectIncident(item.other_incident_id); });
      entry.append(link, node('span', '', ` · ${item.overlapping_traces.length} 条追踪交集`));
      overlaps.append(entry);
    });
    details.append(overlaps);
    appendBlock(left, '调查追踪交集 · 仅供人工核查', details);
  }
  const requestEvidence = node('div');
  requestEvidence.append(node('p', 'small-muted', '以下记录由操作员选择，Span 事实已核验；它不证明聚合告警同根因，也不参与自动修复。'));
  if (data.request_evidence?.length) {
    const list = node('ul');
    data.request_evidence.forEach((item) => {
      const entry = node('li');
      entry.append(
        node('strong', '', `${item.service} · ${item.failure_code}`),
        node('span', '', ` · Trace ${item.trace_id} · Span ${item.span_id}`),
        node('p', '', `核对理由：${item.reason} · ${item.operator_id} · ${date(item.created_at)}`),
      );
      list.append(entry);
    });
    requestEvidence.append(list);
  } else requestEvidence.append(node('p', '', '尚未附加请求级证据。'));
  const attachDetails = node('details');
  attachDetails.append(node('summary', '', '人工附加已核验的请求事件'));
  const attachForm = node('form', 'request-evidence-form');
  const eventInput = node('input');
  eventInput.type = 'text'; eventInput.required = true;
  eventInput.placeholder = '请求事件 UUID'; eventInput.setAttribute('aria-label', '请求事件 UUID');
  const reasonInput = node('input');
  reasonInput.type = 'text'; reasonInput.required = true; reasonInput.minLength = 3;
  reasonInput.maxLength = 300; reasonInput.placeholder = '核对理由'; reasonInput.setAttribute('aria-label', '核对理由');
  const attachButton = node('button', 'secondary-button', '记录人工关联');
  attachButton.type = 'submit';
  attachForm.append(eventInput, reasonInput, attachButton);
  attachForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    attachButton.disabled = true; attachButton.textContent = '正在核对 Span…';
    try {
      await request(`/api/incidents/${data.incident.id}/request-evidence`, {
        method: 'POST',
        body: JSON.stringify({ request_event_id: eventInput.value.trim(), expected_incident_version: data.incident.version, reason: reasonInput.value.trim() }),
      });
      await selectIncident(data.incident.id);
    } catch (error) { attachButton.textContent = `附加失败：${error.message}`; attachButton.disabled = false; }
  });
  attachDetails.append(attachForm); requestEvidence.append(attachDetails);
  appendBlock(left, `请求级证据（${data.request_evidence?.length || 0}）`, requestEvidence);
  const right = node('div');
  const review = data.model_review;
  const reviewArea = node('div', 'detail-actions');
  reviewArea.append(node('span', 'model-chip', review ? `${review.model_name} · ${statusText(review.status)}${review.error_code ? ` · ${review.error_code}` : ''}${review.is_stale ? ' · 已过期' : ''}` : 'DeepSeek 尚未复核'));
  if (report && !report.is_stale && (!review || (review.status === 'FAILED' && review.attempt_count < 2))) {
    const button = node('button', 'secondary-button', review ? '重试 DeepSeek 复核' : '让 DeepSeek 复核证据');
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true; button.textContent = '正在复核…';
      try { await request(`/api/incidents/${state.incidentId}/model-review`, { method: 'POST' }); await selectIncident(state.incidentId); }
      catch (error) { button.textContent = `复核失败：${error.message}`; button.disabled = false; }
    });
    reviewArea.append(button);
  }
  appendBlock(right, '模型复核', reviewArea);
  const advice = data.advice;
  if (advice?.mode === 'MANUAL_INVESTIGATION' && advice.items?.length) {
    const list = node('ul', 'advice-list');
    advice.items.forEach((item) => {
      const entry = node('li');
      entry.append(
        node('strong', '', item.title),
        node('p', '', item.rationale),
        node('p', '', `风险：${item.risk}`),
        node('p', '', `验证：${item.validation}`),
        node('small', '', `依据：${item.cited_evidence_ids.map((id) => id.slice(0, 8)).join('、')}`),
      );
      list.append(entry);
    });
    appendBlock(right, '人工排查建议 · 不可自动执行', list);
  }
  if (review?.summary) appendBlock(right, '复核意见', node('p', '', reviewSummary(review)));
  if (review?.uncertainties?.length) appendBlock(right, '未确定事项', node('p', '', review.uncertainties.join('；')));
  if (data.workflow) appendBlock(right, '工作流', node('p', '', `${statusText(data.workflow.status)} · ${data.workflow.review_state ? statusText(data.workflow.review_state) : '等待中'}`));
  columns.append(left, right); target.append(columns);
  const remediation = node('div', 'remediation-panel');
  remediation.append(node('h3', '', '修复建议与审批'));
  const plan = data.remediation_plan ? planCopy(data.remediation_plan) : null;
  const incidentId = data.incident.id;
  if (plan) {
    const action = plan.actions[0];
    remediation.append(node('p', '', `${statusText(plan.status)}${plan.is_stale ? ' · 已过期' : ''} · ${investigationSummary(plan.root_cause_summary, report?.primary_hypothesis)}`));
    remediation.append(node('p', '', `建议：${plan.rationale} 风险：${plan.risk_summary} 影响：${plan.blast_radius}`));
    if (action) remediation.append(node('p', 'small-muted', `动作 ${action.snapshot.action_type} · ${action.snapshot.target} · ${statusText(action.status)}`));
    if (plan.status === 'PROPOSED' && !plan.is_stale) {
      const button = node('button', 'secondary-button', '提交策略校验并请求审批');
      button.type = 'button';
      button.addEventListener('click', async () => {
        button.disabled = true;
        try { await request(`/api/incidents/${incidentId}/remediation-plan/authorize`, { method: 'POST' }); await selectIncident(incidentId); }
        catch (error) { button.textContent = `策略校验失败：${error.message}`; button.disabled = false; }
      });
      remediation.append(button);
    }
  } else if (report?.status === 'SUPPORTED' && !report.is_stale && advice?.mode === 'GOVERNED_PLAN') {
    const button = node('button', 'secondary-button', '生成受控修复建议');
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await request(`/api/incidents/${incidentId}/remediation-plan`, { method: 'POST' }); await selectIncident(incidentId); }
      catch (error) { remediation.append(node('p', 'error-text', `无法提议：${error.message}`)); button.disabled = false; }
    });
    remediation.append(button);
  } else remediation.append(node('p', '', advice?.mode === 'MANUAL_INVESTIGATION' && report?.status === 'SUPPORTED'
    ? '已识别可能根因，但尚无经过审核的可执行修复动作；请按人工建议核查和恢复。'
    : '当前证据不足或报告已过期；没有可执行的修复建议。建议人工核对近期变更、指标、日志和追踪，补齐可信信号定义后重新调查。'));
  const approval = data.approval;
  if (approval) {
    remediation.append(node('p', '', `人工审批：${statusText(approval.status)} · v${approval.version}${approval.decision_reason ? ` · ${approval.decision_reason}` : ''}`));
    if (approval.status === 'REQUESTED') {
      const reason = node('input', 'approval-reason');
      reason.placeholder = '填写审批原因（至少 3 字）'; reason.maxLength = 300;
      reason.setAttribute('aria-label', '审批原因');
      remediation.append(node('p', 'helper', '开发 Lab 审批身份。批准后，独立执行器可能立即在 Lab 执行动作。'));
      remediation.append(reason);
      for (const decision of ['APPROVE', 'REJECT']) {
        const button = node('button', 'secondary-button', decision === 'APPROVE' ? '批准并允许 Lab 执行' : '拒绝');
        button.type = 'button';
        button.addEventListener('click', async () => {
          if (reason.value.trim().length < 3) { reason.focus(); return; }
          button.disabled = true;
          try {
            await request(`/api/approvals/${approval.id}/decision`, { method: 'POST', body: JSON.stringify({ expected_version: approval.version, decision, reason: reason.value.trim() }) });
            await selectIncident(incidentId);
          } catch (error) { button.textContent = `审批失败：${error.message}`; button.disabled = false; }
        });
        remediation.append(button);
      }
    }
  }
  const recovery = data.recovery;
  if (recovery) {
    remediation.append(node('p', 'small-muted', `恢复评估：${recovery.recovery_ready ? '已就绪' : '未就绪'}${recovery.limitations?.length ? ` · ${recovery.limitations.join('；')}` : ''}`));
    const resolvable = (recovery.alerts ?? []).find(alert =>
      alert.status === 'OBSERVED' && alert.name === 'VerifiedRequestFailureWindow');
    if (resolvable && data.incident.status === 'OPEN') {
      const resolveReason = node('input', 'approval-reason');
      resolveReason.placeholder = '填写核实依据（症状已结束的证据，至少 3 字）';
      resolveReason.maxLength = 300;
      resolveReason.setAttribute('aria-label', '告警核实解除原因');
      const resolveButton = node('button', 'secondary-button', '核实解除症状告警');
      resolveButton.type = 'button';
      resolveButton.addEventListener('click', async () => {
        if (resolveReason.value.trim().length < 3) { resolveReason.focus(); return; }
        resolveButton.disabled = true;
        try {
          await request(`/api/alerts/${resolvable.id}/resolve`, { method: 'POST', body: JSON.stringify({ reason: resolveReason.value.trim() }) });
          await selectIncident(incidentId);
        } catch (error) { resolveButton.textContent = `解除失败：${error.message}`; resolveButton.disabled = false; }
      });
      remediation.append(node('p', 'helper', '提升的症状告警没有自动解除通道；操作员核实症状已结束后在此解除，恢复评估才会就绪。'));
      remediation.append(resolveReason, resolveButton);
    }
  }
  target.append(remediation);
  const timeline = node('div', 'detail-block');
  timeline.append(node('h3', '', '审计时间线'));
  data.timeline.slice(-10).reverse().forEach((event) => {
    const item = node('div', 'timeline-item');
    item.append(node('strong', '', `${event.event_type} · ${event.summary}`), node('small', '', date(event.created_at)));
    timeline.append(item);
  });
  target.append(timeline);
}

async function selectIncident(id) {
  if (!id) return;
  state.incidentId = id;
  state.detailLoaded = true;
  const requestId = ++state.detailRequestId;
  if (!$('ops-view').classList.contains('hidden')) {
    const url = new URL(location.href);
    url.searchParams.set('view', 'ops');
    url.searchParams.set('incident', id);
    history.replaceState(null, '', url);
  }
  renderIncidents();
  $('incident-detail').className = 'detail-empty';
  $('incident-detail').textContent = '正在读取事故调查…';
  try {
    const detail = await request(`/api/incidents/${id}`);
    if (requestId === state.detailRequestId) renderDetail(detail);
  } catch (error) {
    if (requestId === state.detailRequestId) $('incident-detail').textContent = `读取失败：${error.message}。请点击“刷新现场”重试。`;
  }
}

document.querySelectorAll('.nav-button').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view)));
$('open-ops').addEventListener('click', () => showView('ops'));
$('refresh-button').addEventListener('click', async () => { await refreshOverview(); if (state.incidentId) await selectIncident(state.incidentId); });
$('incident-filter').addEventListener('input', () => { if (state.overview) renderIncidents(); });
$('quantity').addEventListener('change', () => { quantity(); state.pendingKey = null; updateCheckout(); });
$('qty-minus').addEventListener('click', () => { $('quantity').value = String(Math.max(1, quantity() - 1)); state.pendingKey = null; updateCheckout(); });
$('qty-plus').addEventListener('click', () => { $('quantity').value = String(Math.min(100, quantity() + 1)); state.pendingKey = null; updateCheckout(); });
$('checkout-button').addEventListener('click', checkout);
$('lab-alert-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('lab-alert-button');
  const result = $('lab-alert-result');
  const fields = new FormData(event.currentTarget);
  const body = Object.fromEntries(fields.entries());
  for (const field of ['resource', 'signal_type']) if (!body[field]) delete body[field];
  button.disabled = true;
  result.textContent = '正在送入告警链路…';
  try {
    const accepted = await request('/api/lab-alerts', { method: 'POST', body: JSON.stringify(body) });
    result.textContent = `已接收 · ${accepted.fingerprint.slice(0, 12)}，稍后刷新事故列表`;
    await refreshOverview();
  } catch (error) { result.textContent = `发送失败：${error.message}`; }
  finally { button.disabled = false; }
});
loadCatalog(); refreshOverview();
if (new URLSearchParams(location.search).get('view') === 'ops') showView('ops');
setInterval(() => { if (!$('ops-view').classList.contains('hidden')) refreshOverview(); }, 6000);

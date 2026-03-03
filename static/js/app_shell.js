(function () {
  'use strict';

  let providerCache = null;

  function escText(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function fallbackProviderCatalog() {
    const labels = window._PROVIDER_LABELS || {};
    return Object.entries(labels).map(([value, label]) => ({
      value,
      label,
      configured: true,
    }));
  }

  async function fetchProviderCatalog(force) {
    if (providerCache && !force) {
      return providerCache;
    }
    if (typeof window.api !== 'function') {
      providerCache = {
        all: fallbackProviderCatalog(),
        configured: fallbackProviderCatalog(),
      };
      return providerCache;
    }
    const data = await window.api('/api/import/aikeys');
    if (
      data &&
      !window.isApiError?.(data) &&
      Array.isArray(data.provider_catalog)
    ) {
      providerCache = {
        all: data.provider_catalog,
        configured: Array.isArray(data.configured_providers) && data.configured_providers.length
          ? data.configured_providers
          : data.provider_catalog,
      };
      return providerCache;
    }
    providerCache = {
      all: fallbackProviderCatalog(),
      configured: fallbackProviderCatalog(),
    };
    return providerCache;
  }

  function renderProviderSelect(selectEl, options) {
    if (!selectEl) {
      return;
    }
    const {
      pool,
      selected,
      includeBlank,
      blankLabel,
    } = options;
    const rows = [];
    if (includeBlank) {
      rows.push(`<option value="">${escText(blankLabel || '-- Use global default --')}</option>`);
    }
    for (const item of pool) {
      const label = item.configured === false ? `${item.label} (not configured)` : item.label;
      rows.push(`<option value="${escText(item.value)}">${escText(label)}</option>`);
    }
    selectEl.innerHTML = rows.join('');
    const valid = Array.from(selectEl.options).some((opt) => opt.value === selected);
    if (valid) {
      selectEl.value = selected;
      return;
    }
    if (includeBlank) {
      selectEl.value = '';
      return;
    }
    if (pool[0]) {
      selectEl.value = pool[0].value;
    }
  }

  function providerPoolForSelection(catalog, selected) {
    const base = catalog.configured.length ? catalog.configured.slice() : catalog.all.slice();
    if (selected && !base.some((item) => item.value === selected)) {
      const fallback = catalog.all.find((item) => item.value === selected);
      if (fallback) {
        base.push(fallback);
      }
    }
    return base;
  }

  async function syncOfficeProviderSelect(force) {
    const selectEl = document.getElementById('ofc-provider-sel');
    if (!selectEl) {
      return;
    }
    const catalog = await fetchProviderCatalog(force);
    const selected = selectEl.value || document.getElementById('aip-default-provider')?.value || '';
    renderProviderSelect(selectEl, {
      pool: providerPoolForSelection(catalog, selected),
      selected,
      includeBlank: false,
    });
    if (typeof window.officeUpdateModelSel === 'function') {
      window.officeUpdateModelSel();
    }
  }

  async function syncInstructionProviderSelect(force) {
    const selectEl = document.getElementById('emp-instr-ai-provider');
    if (!selectEl) {
      return;
    }
    const current = selectEl.value || '';
    const catalog = await fetchProviderCatalog(force);
    renderProviderSelect(selectEl, {
      pool: providerPoolForSelection(catalog, current),
      selected: current,
      includeBlank: true,
      blankLabel: '-- Use global default --',
    });
    if (typeof window.empInstrUpdateModelSel === 'function') {
      window.empInstrUpdateModelSel();
    }
  }

  async function syncProviderUi(force) {
    await Promise.all([
      syncOfficeProviderSelect(force),
      syncInstructionProviderSelect(force),
    ]);
  }

  function applySidebarOrder() {
    const nav = document.querySelector('#sidebar nav');
    if (!nav) {
      return;
    }
    const items = new Map(
      Array.from(nav.querySelectorAll('.nav-item[data-page]')).map((node) => [node.dataset.page, node]),
    );
    const mapSection = nav.querySelector('.map-section');
    const extHeader = nav.querySelector('.ext-section-hdr');
    const desiredPages = [
      'alarms',
      'map',
      'hosts',
      'office',
      'incidents',
      'escalations',
      'workflows',
      'watchlist',
      'runbooks',
      'sla',
      'intel',
      'observability-summary',
      'observability-zabbix',
      'observability-grafana',
      'observability-kuma',
      'sites',
      'changes',
      'alert-rules',
      'hreality',
      'settings',
    ];

    const fragment = document.createDocumentFragment();
    const appended = new Set();
    for (const page of desiredPages) {
      const node = items.get(page);
      if (!node) {
        continue;
      }
      fragment.appendChild(node);
      appended.add(node);
      if (page === 'map' && mapSection) {
        fragment.appendChild(mapSection);
      }
      if (page === 'intel' && extHeader) {
        extHeader.textContent = 'Observability';
        fragment.appendChild(extHeader);
      }
    }

    const leftovers = Array.from(nav.children).filter(
      (node) => !appended.has(node) && node !== mapSection && node !== extHeader,
    );
    for (const node of leftovers) {
      fragment.appendChild(node);
    }
    nav.appendChild(fragment);

    const mapLabel = items.get('map')?.querySelector('.nav-label');
    if (mapLabel) {
      mapLabel.textContent = 'Maps';
    }
    const defaultMapLabel = nav.querySelector('.map-list-item[data-mapid="0"] span:last-child');
    if (defaultMapLabel) {
      defaultMapLabel.textContent = 'Primary Map';
    }
    const titleEl = document.getElementById('page-title');
    if (titleEl && document.querySelector('.nav-item.active[data-page="map"]')) {
      titleEl.textContent = 'Maps';
    }
  }

  function focusObservabilitySettingsCard() {
    const card = document.getElementById('obs-settings-card');
    if (!card) {
      return;
    }
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.style.boxShadow = '0 0 0 1px rgba(0,212,255,0.45), 0 0 24px rgba(0,212,255,0.12)';
    window.setTimeout(() => {
      card.style.boxShadow = '';
    }, 2200);
  }

  function updateObservabilityAccessButton() {
    const btn = document.getElementById('obs-access-btn');
    if (!btn) {
      return;
    }
    const grafanaUser = document.getElementById('obs-cfg-grafana-user')?.value || '';
    const zabbixUser = document.getElementById('obs-cfg-zabbix-user')?.value || '';
    const kumaUrl = document.getElementById('obs-cfg-kuma-url')?.value || '';
    const summary = [
      grafanaUser ? `Grafana: ${grafanaUser}` : 'Grafana: no user',
      zabbixUser ? `Zabbix: ${zabbixUser}` : 'Zabbix: no user',
      kumaUrl ? 'Kuma: set' : 'Kuma: no URL',
    ].join(' | ');
    btn.title = summary;
  }

  async function appendSnapshotInsight() {
    const panel = document.getElementById('obs-pulse');
    if (!panel || typeof window.api !== 'function') {
      return;
    }
    const existing = document.getElementById('obs-shell-insight');
    if (existing) {
      existing.remove();
    }
    const data = await window.api('/api/observability/overview');
    if (!data || window.isApiError?.(data) || !data.summary) {
      return;
    }
    const snapshot = data.snapshot || {};
    const summary = data.summary || {};
    const sources = data.sources || {};
    const kumaSync = data.kuma_sync || {};
    const activeTab = document.querySelector('#page-observability .filter-btn.active');
    const currentTarget = activeTab?.id?.replace('obs-tab-', '') || 'summary';
    const targetState = (snapshot.dashboards || {})[currentTarget] || {};
    const topProblems = ((snapshot.zabbix || {}).top_problems || []).slice(0, 3);
    const accent = summary.overall_status === 'critical'
      ? '#f87171'
      : summary.overall_status === 'degraded'
        ? '#fbbf24'
        : '#4ade80';
    const list = topProblems.length
      ? `<div style="margin-top:8px;color:#d6e4fb">${topProblems.map((item) => `- ${escText(item.name || 'Unnamed problem')}`).join('<br>')}</div>`
      : '';
    const zbx = sources.zabbix || {};
    const grafana = sources.grafana || {};
    const kuma = sources.kuma || {};
    const sourceGrid = `
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px">
        <div style="padding:8px;border-radius:10px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05)">
          <div style="font-size:9px;text-transform:uppercase;letter-spacing:0.6px;color:var(--muted)">Zabbix</div>
          <div style="margin-top:4px;font-size:13px;font-weight:700;color:${(zbx.problem_count || 0) ? '#f87171' : '#4ade80'}">${escText(String(zbx.problem_count || 0))} problems</div>
          <div style="margin-top:3px;color:#9fb3cf;font-size:10px">${escText(String(zbx.host_count || 0))} hosts</div>
        </div>
        <div style="padding:8px;border-radius:10px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05)">
          <div style="font-size:9px;text-transform:uppercase;letter-spacing:0.6px;color:var(--muted)">Grafana</div>
          <div style="margin-top:4px;font-size:13px;font-weight:700;color:${['invalid_credentials', 'interactive_login_required'].includes(grafana.auth_status) ? '#fbbf24' : grafana.status === 'ok' ? '#4ade80' : '#f87171'}">${escText(grafana.status || 'unknown')}</div>
          <div style="margin-top:3px;color:#9fb3cf;font-size:10px">${escText(grafana.version || grafanaAuthStateLabel(grafana) || 'no version')}</div>
        </div>
        <div style="padding:8px;border-radius:10px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05)">
          <div style="font-size:9px;text-transform:uppercase;letter-spacing:0.6px;color:var(--muted)">Kuma</div>
          <div style="margin-top:4px;font-size:13px;font-weight:700;color:${kuma.status === 'ok' ? '#4ade80' : '#fbbf24'}">${escText(kuma.status_text || kuma.status || 'unknown')}</div>
          <div style="margin-top:3px;color:#9fb3cf;font-size:10px">${escText(summary.recommended_kuma_state || 'up')}</div>
        </div>
      </div>
    `;
    const syncLine = kumaSync && kumaSync.url
      ? `<div style="margin-top:8px;color:#9fb3cf;font-size:10px">Kuma sync: ${escText(kumaSync.status || 'unknown')} via ${escText(kumaSync.url)}</div>`
      : '';
    panel.insertAdjacentHTML(
      'beforeend',
      `
        <div id="obs-shell-insight" style="margin-top:12px;padding:10px;border-radius:10px;border:1px solid ${accent}33;background:rgba(8,16,30,0.6);line-height:1.6">
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.7px;color:var(--muted)">Smart Context</div>
          <div style="margin-top:5px;color:#e5eefc;font-weight:600">${escText(summary.headline || 'No summary available.')}</div>
          <div style="margin-top:6px;color:#b8c9e0">${currentTarget === 'summary' ? 'Current view: unified monitoring board.' : `Current view: ${escText(currentTarget)} is ${escText(targetState.status || 'unknown')}.`}</div>
          <div style="margin-top:4px;color:#b8c9e0">Recommended Kuma state: ${escText(summary.recommended_kuma_state || 'up')}.</div>
          ${sourceGrid}
          ${syncLine}
          ${list}
        </div>
      `,
    );
  }

  function wrapFunction(name, wrapper) {
    const original = window[name];
    if (typeof original !== 'function') {
      return;
    }
    window[name] = wrapper(original);
  }

  function currentObsTarget() {
    const activeTab = document.querySelector('#page-observability .filter-btn.active');
    return activeTab?.id?.replace('obs-tab-', '') || 'summary';
  }

  function grafanaAuthStateLabel(source) {
    const state = String(source?.auth_status || 'unknown');
    if (state === 'interactive_login_required') {
      return 'browser login required';
    }
    if (state === 'invalid_credentials') {
      return 'api auth rejected';
    }
    return state.replace(/_/g, ' ');
  }

  function sourceStatusColor(status, okColor = '#4ade80', warnColor = '#fbbf24', errorColor = '#f87171') {
    const raw = String(status || 'unknown');
    if (['ok', 'operational', 'healthy', 'aligned'].includes(raw)) {
      return okColor;
    }
    if (['degraded', 'warning', 'mismatch', 'http_404', 'unsupported'].includes(raw)) {
      return warnColor;
    }
    return errorColor;
  }

  function formatKumaSyncMessage(kumaSync) {
    if (!kumaSync || !kumaSync.url) {
      return 'Kuma sync has not been attempted yet.';
    }
    if (kumaSync.ok) {
      return 'Kuma accepted the latest NOC status override.';
    }
    if (kumaSync.status === 'http_404') {
      return 'Kuma exposes /api/status but is missing /api/sentinel/override. Deploy the updated status-page server on that VM to enable automatic status alignment.';
    }
    return String(kumaSync.detail || kumaSync.status || 'Kuma sync is failing.');
  }

  function kumaPublicStateLabel(kuma) {
    return String(kuma?.status_text || kuma?.page_state || kuma?.status || 'Unknown');
  }

  window.obsOpenNamedExternal = function (target) {
    const url = typeof window.obsGetUrl === 'function' ? window.obsGetUrl(target) : '';
    if (!url) {
      window.showToast?.(`No ${target} URL is configured.`, 'warn');
      return;
    }
    window.open(url, '_blank', 'noopener,noreferrer');
  };

  window.obsManualKumaSync = async function () {
    if (typeof window.api !== 'function') {
      return;
    }
    const result = await window.api('/api/observability/sync-kuma', { method: 'POST' });
    if (window.isApiError?.(result)) {
      window.showToast?.(window.apiMessage?.(result, 'Kuma sync failed.'), 'error');
    } else {
      window.showToast?.('Kuma sync requested.', 'success');
    }
    if (typeof window.renderObservabilityHomeBoard === 'function') {
      await window.renderObservabilityHomeBoard();
    }
    if (typeof window.loadObservabilityPulse === 'function') {
      await window.loadObservabilityPulse();
    }
  };

  async function renderObservabilityHomeBoard() {
    const data = await window.api?.('/api/observability/overview');
    if (!data || window.isApiError?.(data) || !data.sources) {
      return false;
    }
    const overlay = document.getElementById('obs-frame-overlay');
    const frame = document.getElementById('obs-frame');
    if (!overlay || !frame) {
      return false;
    }

    const summary = data.summary || {};
    const sources = data.sources || {};
    const zabbix = sources.zabbix || {};
    const grafana = sources.grafana || {};
    const kuma = sources.kuma || {};
    const kumaSync = data.kuma_sync || {};
    const overallColor = sourceStatusColor(summary.overall_status);
    const topProblems = Array.isArray(zabbix.top_problems) ? zabbix.top_problems.slice(0, 5) : [];
    const dashboards = Array.isArray(grafana.dashboards) ? grafana.dashboards.slice(0, 6) : [];
    const kumaGroups = Array.isArray(kuma.groups) ? kuma.groups.slice(0, 6) : [];
    const actions = Array.isArray(summary.actions) ? summary.actions.slice(0, 5) : [];
    const syncMessage = formatKumaSyncMessage(kumaSync);
    const syncTone = sourceStatusColor(kumaSync.ok ? 'ok' : kumaSync.status || 'warning');

    frame.style.display = 'none';
    frame.removeAttribute('src');
    overlay.style.display = 'flex';
    overlay.style.alignItems = 'stretch';
    overlay.style.justifyContent = 'stretch';
    overlay.innerHTML = `
      <div style="width:100%;height:100%;border:1px solid rgba(0,212,255,0.16);border-radius:16px;padding:18px 20px;background:linear-gradient(180deg,rgba(8,12,24,0.98),rgba(6,10,20,0.98));box-shadow:0 18px 40px rgba(0,0,0,0.35);overflow:auto">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap">
          <div>
            <div style="font-size:14px;font-weight:700;color:#fff">Monitoring Home</div>
            <div style="margin-top:6px;color:#9fb3cf;font-size:11px;line-height:1.7">Centralized NOC state using live Zabbix alarms, Grafana API data, and Kuma public status.</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.7px;color:var(--muted)">Overall</div>
            <div style="margin-top:5px;font-size:14px;font-weight:700;color:${overallColor}">${escText(String(summary.overall_status || 'unknown').toUpperCase())}</div>
          </div>
        </div>

        <div style="margin-top:14px;padding:12px 14px;border-radius:12px;border:1px solid ${overallColor}33;background:rgba(255,255,255,0.02)">
          <div style="font-size:11px;font-weight:700;color:#e5eefc">Current Signal</div>
          <div style="margin-top:6px;color:#dbe8fb;font-size:11px;line-height:1.7">${escText(summary.headline || 'No active issues detected.')}</div>
          <div style="margin-top:8px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px">
            <div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px">Zabbix</div><div style="margin-top:4px;font-size:12px;font-weight:700;color:${sourceStatusColor((zabbix.problem_count || 0) ? 'error' : 'ok')}">${escText(String(zabbix.problem_count || 0))} problems</div></div>
            <div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px">Grafana</div><div style="margin-top:4px;font-size:12px;font-weight:700;color:${sourceStatusColor(grafana.status)}">${escText(grafana.status || 'unknown')}</div></div>
            <div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px">Kuma Public</div><div style="margin-top:4px;font-size:12px;font-weight:700;color:${sourceStatusColor(summary.kuma_alignment)}">${escText(kumaPublicStateLabel(kuma))}</div></div>
            <div><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px">Expected Kuma</div><div style="margin-top:4px;font-size:12px;font-weight:700;color:#7dd3fc">${escText(String(summary.recommended_kuma_state || 'up').replace(/_/g, ' '))}</div></div>
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:14px;margin-top:14px;align-items:start">
          <div style="border:1px solid rgba(255,255,255,0.06);border-radius:12px;background:rgba(255,255,255,0.02);padding:12px 14px">
            <div style="font-size:11px;font-weight:700;color:#fff">Zabbix Signal</div>
            <div style="margin-top:8px;display:grid;grid-template-columns:1fr auto;gap:8px;color:#dbe8fb;font-size:11px">
              <div style="color:var(--muted)">Hosts</div><div>${escText(String(zabbix.host_count || 0))}</div>
              <div style="color:var(--muted)">Active Problems</div><div style="color:${(zabbix.problem_count || 0) ? '#f87171' : '#4ade80'}">${escText(String(zabbix.problem_count || 0))}</div>
              <div style="color:var(--muted)">Critical</div><div>${escText(String(zabbix.critical_problem_count || 0))}</div>
            </div>
            <div style="margin-top:10px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px">Top Problems</div>
            <div style="margin-top:6px;color:#dbe8fb;font-size:11px;line-height:1.7">${topProblems.length ? topProblems.map((item) => `- ${escText(item.host || 'host')} :: ${escText(item.name || 'Unnamed problem')}`).join('<br>') : 'No active Zabbix problems.'}</div>
          </div>

          <div style="border:1px solid rgba(255,255,255,0.06);border-radius:12px;background:rgba(255,255,255,0.02);padding:12px 14px">
            <div style="font-size:11px;font-weight:700;color:#fff">Grafana Signal</div>
            <div style="margin-top:8px;display:grid;grid-template-columns:1fr auto;gap:8px;color:#dbe8fb;font-size:11px">
              <div style="color:var(--muted)">Service</div><div style="color:${sourceStatusColor(grafana.status)}">${escText(grafana.status || 'unknown')}</div>
              <div style="color:var(--muted)">API Health</div><div>${escText(grafana.api_health || 'unknown')}</div>
              <div style="color:var(--muted)">Auth</div><div>${escText(grafanaAuthStateLabel(grafana))}</div>
              <div style="color:var(--muted)">Version</div><div>${escText(grafana.version || 'unknown')}</div>
              <div style="color:var(--muted)">Dashboards</div><div>${escText(String(grafana.dashboard_count || 0))}</div>
            </div>
            <div style="margin-top:10px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px">Available Dashboards</div>
            <div style="margin-top:6px;color:#dbe8fb;font-size:11px;line-height:1.7">${dashboards.length ? dashboards.map((item) => `- ${escText(item.title || 'Untitled')}`).join('<br>') : 'No dashboard list available.'}</div>
          </div>

          <div style="border:1px solid rgba(255,255,255,0.06);border-radius:12px;background:rgba(255,255,255,0.02);padding:12px 14px">
            <div style="font-size:11px;font-weight:700;color:#fff">Kuma Public State</div>
            <div style="margin-top:8px;display:grid;grid-template-columns:1fr auto;gap:8px;color:#dbe8fb;font-size:11px">
              <div style="color:var(--muted)">Public State</div><div style="color:${sourceStatusColor(summary.kuma_alignment)}">${escText(kumaPublicStateLabel(kuma))}</div>
              <div style="color:var(--muted)">API Status</div><div>${escText(kuma.api_status || kuma.status || 'unknown')}</div>
              <div style="color:var(--muted)">Groups</div><div>${escText(String(kuma.group_count || 0))}</div>
              <div style="color:var(--muted)">Public Problems</div><div>${escText(String(kuma.problem_count || 0))}</div>
            </div>
            <div style="margin-top:10px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px">Public Group States</div>
            <div style="margin-top:6px;color:#dbe8fb;font-size:11px;line-height:1.7">${kumaGroups.length ? kumaGroups.map((item) => `- ${escText(item.label || 'Group')} :: ${escText(String(item.status || 'unknown').replace(/_/g, ' '))}`).join('<br>') : 'No grouped public status available.'}</div>
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1.2fr 1fr;gap:14px;margin-top:14px;align-items:start">
          <div style="border:1px solid rgba(255,255,255,0.06);border-radius:12px;background:rgba(255,255,255,0.02);padding:12px 14px">
            <div style="font-size:11px;font-weight:700;color:#fff">Cross-Source Coordination</div>
            <div style="margin-top:8px;color:#dbe8fb;font-size:11px;line-height:1.7">${actions.length ? actions.map((item) => `- ${escText(item)}`).join('<br>') : 'No immediate operator action suggested.'}</div>
            <div style="margin-top:10px;color:#dbe8fb;font-size:11px;line-height:1.7">${Array.isArray(summary.abnormalities) && summary.abnormalities.length ? summary.abnormalities.map((item) => `- ${escText(item)}`).join('<br>') : ''}</div>
          </div>
          <div style="border:1px solid rgba(255,255,255,0.06);border-radius:12px;background:rgba(255,255,255,0.02);padding:12px 14px">
            <div style="font-size:11px;font-weight:700;color:#fff">Kuma Sync</div>
            <div style="margin-top:8px;font-size:12px;font-weight:700;color:${syncTone}">${escText(kumaSync.ok ? 'SYNCED' : String(kumaSync.status || 'unknown').toUpperCase())}</div>
            <div style="margin-top:8px;color:#dbe8fb;font-size:11px;line-height:1.7">${escText(syncMessage)}</div>
          </div>
        </div>

        <div style="display:flex;gap:10px;margin-top:16px;flex-wrap:wrap">
          <button class="tb-btn" onclick="obsLoadFrame(true)" style="margin-left:0">Refresh Board</button>
          <button class="tb-btn" onclick="obsManualKumaSync()" style="margin-left:0">Sync Kuma</button>
          <button class="tb-btn" onclick="obsSelectTarget('zabbix')" style="margin-left:0">Drill Into Zabbix</button>
          <button class="tb-btn" onclick="obsSelectTarget('grafana')" style="margin-left:0">Drill Into Grafana</button>
          <button class="tb-btn" onclick="obsSelectTarget('kuma')" style="margin-left:0">Drill Into Kuma</button>
          <button class="btn btn-primary" onclick="openObservabilityAccessSettings()" style="font-size:11px;padding:6px 14px">Edit Access</button>
        </div>
      </div>
    `;
    return true;
  }

  window.renderObservabilityHomeBoard = renderObservabilityHomeBoard;

  async function renderObservabilitySmartFallback(target, metaText, preflight) {
    const data = await window.api?.('/api/observability/overview');
    if (!data || window.isApiError?.(data) || !data.sources) {
      return false;
    }
    const overlay = document.getElementById('obs-frame-overlay');
    const frame = document.getElementById('obs-frame');
    if (!overlay || !frame) {
      return false;
    }

    const summary = data.summary || {};
    const sources = data.sources || {};
    const source = sources[target] || {};
    const kumaSync = data.kuma_sync || {};
    const zabbix = sources.zabbix || {};
    const grafana = sources.grafana || {};
    const kuma = sources.kuma || {};
    const rows = [];
    const notes = [];

    if (preflight?.requires_login) {
      notes.push('The upstream dashboard is returning a login page, so the browser cannot show it inline safely.');
      if (preflight?.auth_used) {
        notes.push('Stored server-side credentials were tried, but the upstream still presented an interactive sign-in flow.');
      }
    }
    if (preflight?.frame_allowed === false && preflight?.frame_reason) {
      notes.push(`Embedding is blocked by upstream frame policy (${preflight.frame_reason}).`);
    }
    if (preflight?.client_frame_bust) {
      notes.push('The upstream page also contains client-side anti-frame logic.');
    }

    if (target === 'zabbix') {
      rows.push(['API Status', source.status || 'unknown']);
      rows.push(['Host Count', String(source.host_count || 0)]);
      rows.push(['Active Problems', String(source.problem_count || 0)]);
      rows.push(['Critical Problems', String(source.critical_problem_count || 0)]);
      const problems = Array.isArray(source.top_problems) ? source.top_problems.slice(0, 4) : [];
      if (problems.length) {
        rows.push(['Top Problems', problems.map((item) => item.name || 'Unnamed').join(' | ')]);
      }
    } else if (target === 'grafana') {
      rows.push(['Service Status', source.status || 'unknown']);
      rows.push(['API Health', source.api_health || 'unknown']);
      rows.push(['Auth Status', grafanaAuthStateLabel(source)]);
      rows.push(['Version', source.version || 'unknown']);
      if (source.dashboard_count != null) {
        rows.push(['Dashboards', String(source.dashboard_count || 0)]);
      }
      const titles = Array.isArray(source.dashboards) ? source.dashboards.slice(0, 4).map((item) => item.title || 'Untitled') : [];
      if (titles.length) {
        rows.push(['Top Dashboards', titles.join(' | ')]);
      }
      rows.push(['Workspace Access', source.dashboard_access || 'unknown']);
      if (source.auth_hint) {
        rows.push(['Auth Note', source.auth_hint]);
      }
    } else if (target === 'kuma') {
      rows.push(['Page Status', source.status || 'unknown']);
      if (source.page_state) {
        rows.push(['Public State', source.page_state]);
      }
      rows.push(['Status Text', source.status_text || 'unknown']);
      rows.push(['Public Problems', String(source.problem_count || 0)]);
      rows.push(['Groups', String(source.group_count || 0)]);
      rows.push(['Recommended State', source.recommended_state || summary.recommended_kuma_state || 'up']);
      rows.push(['Recommended Note', source.recommended_note || summary.recommended_kuma_note || '']);
    }

    if (kumaSync && kumaSync.url) {
      rows.push(['Kuma Sync', `${kumaSync.status || 'unknown'} via ${kumaSync.url}`]);
    }

    const details = rows.map(([label, value]) => `
      <div style="display:flex;justify-content:space-between;gap:14px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
        <span style="color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:0.6px">${escText(label)}</span>
        <span style="color:#e5eefc;font-size:11px;text-align:right;max-width:60%;line-height:1.5">${escText(value || 'n/a')}</span>
      </div>
    `).join('');

    const abnormalList = Array.isArray(summary.abnormalities) && summary.abnormalities.length
      ? `<div style="margin-top:12px;color:#d8e4fa;font-size:11px;line-height:1.7">${summary.abnormalities.map((item) => `- ${escText(item)}`).join('<br>')}</div>`
      : '';
    const noteBlock = notes.length
      ? `<div style="margin-top:12px;color:#f9d67a;font-size:11px;line-height:1.7">${notes.map((item) => `- ${escText(item)}`).join('<br>')}</div>`
      : '';
    const sourceStrip = `
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px">
        <div style="border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:10px 12px;background:rgba(255,255,255,0.02)">
          <div style="font-size:9px;text-transform:uppercase;letter-spacing:0.7px;color:var(--muted)">Zabbix</div>
          <div style="margin-top:6px;font-size:13px;font-weight:700;color:${(zabbix.problem_count || 0) ? '#f87171' : '#4ade80'}">${escText(String(zabbix.problem_count || 0))} problems</div>
          <div style="margin-top:4px;font-size:10px;color:#9fb3cf">${escText(String(zabbix.host_count || 0))} hosts</div>
        </div>
        <div style="border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:10px 12px;background:rgba(255,255,255,0.02)">
          <div style="font-size:9px;text-transform:uppercase;letter-spacing:0.7px;color:var(--muted)">Grafana</div>
          <div style="margin-top:6px;font-size:13px;font-weight:700;color:${['invalid_credentials', 'interactive_login_required'].includes(grafana.auth_status) ? '#fbbf24' : grafana.status === 'ok' ? '#4ade80' : '#f87171'}">${escText(grafana.status || 'unknown')}</div>
          <div style="margin-top:4px;font-size:10px;color:#9fb3cf">${escText(grafanaAuthStateLabel(grafana))}</div>
        </div>
        <div style="border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:10px 12px;background:rgba(255,255,255,0.02)">
          <div style="font-size:9px;text-transform:uppercase;letter-spacing:0.7px;color:var(--muted)">Kuma</div>
          <div style="margin-top:6px;font-size:13px;font-weight:700;color:${kuma.status === 'ok' ? '#4ade80' : '#fbbf24'}">${escText(kuma.status_text || kuma.status || 'unknown')}</div>
          <div style="margin-top:4px;font-size:10px;color:#9fb3cf">${escText(summary.recommended_kuma_state || 'up')}</div>
        </div>
      </div>
    `;

    frame.style.display = 'none';
    frame.removeAttribute('src');
    overlay.style.display = 'flex';
    overlay.style.alignItems = 'stretch';
    overlay.style.justifyContent = 'stretch';
    overlay.innerHTML = `
      <div style="width:100%;height:100%;border:1px solid rgba(0,212,255,0.16);border-radius:16px;padding:18px 20px;background:linear-gradient(180deg,rgba(8,12,24,0.98),rgba(6,10,20,0.98));box-shadow:0 18px 40px rgba(0,0,0,0.35);overflow:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
          <div>
            <div style="font-size:14px;font-weight:700;color:#fff">Monitoring Board</div>
            <div style="margin-top:6px;color:#9fb3cf;font-size:11px;line-height:1.7">
              ${escText(target.toUpperCase())} is in smart summary mode. NOC Sentinel is using live server-side checks instead of a blocked iframe.
            </div>
          </div>
          <div style="font-size:11px;color:#7dd3fc">${escText(metaText || '')}</div>
        </div>
        ${sourceStrip}
        <div style="margin-top:14px;padding:12px 14px;border-radius:12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05)">
          <div style="font-size:11px;font-weight:700;color:#e5eefc">Current Signal</div>
          <div style="margin-top:5px;font-size:11px;color:#dbe8fb;line-height:1.7">${escText(summary.headline || 'No active overview headline.')}</div>
          ${details}
          ${noteBlock}
          ${abnormalList}
        </div>
        <div style="display:flex;gap:10px;margin-top:16px;flex-wrap:wrap">
          <button class="btn btn-primary" onclick="obsOpenExternal()" style="font-size:11px;padding:6px 14px">Open Externally</button>
          <button class="tb-btn" onclick="obsLoadFrame(true)" style="margin-left:0">Refresh Summary</button>
          <button class="tb-btn" onclick="openObservabilityAccessSettings()" style="margin-left:0">Edit Access</button>
        </div>
      </div>
    `;
    return true;
  }

  window.renderObservabilitySmartFallback = renderObservabilitySmartFallback;

  window.openObservabilityAccessSettings = function () {
    if (typeof window.navigate === 'function') {
      window.navigate('settings');
    }
    window.setTimeout(focusObservabilitySettingsCard, 180);
  };

  if (typeof window.obsConfiguredDefaults === 'function') {
    window.obsConfiguredDefaults = function () {
      const grafanaUrl = document.getElementById('obs-cfg-grafana-url')?.value?.trim() || '';
      const zabbixUrl = document.getElementById('obs-cfg-zabbix-url')?.value?.trim() || '';
      const kumaUrl = document.getElementById('obs-cfg-kuma-url')?.value?.trim() || '';
      return {
        grafana: grafanaUrl || 'https://grafana.tabadul.iq/dashboards',
        zabbix: zabbixUrl || 'https://zabbix.tabadul.iq/zabbix.php?action=dashboard.view&dashboardid=1&from=now-15m&to=now',
        kuma: kumaUrl || '',
      };
    };
  }

  wrapFunction('loadAiKeys', (original) => async function () {
    const result = await original.apply(this, arguments);
    await syncProviderUi(true);
    return result;
  });

  wrapFunction('renderOfficePage', (original) => function () {
    const result = original.apply(this, arguments);
    window.setTimeout(() => {
      syncOfficeProviderSelect(false);
    }, 0);
    return result;
  });

  wrapFunction('init', (original) => async function () {
    const result = await original.apply(this, arguments);
    if (typeof window.navigate === 'function') {
      window.navigate('observability-summary');
    }
    return result;
  });

  wrapFunction('openInstrModal', (original) => async function () {
    const result = await original.apply(this, arguments);
    await syncInstructionProviderSelect(false);
    return result;
  });

  wrapFunction('loadObservabilityConfig', (original) => async function () {
    const result = await original.apply(this, arguments);
    updateObservabilityAccessButton();
    return result;
  });

  wrapFunction('loadObservabilityPulse', (original) => async function () {
    const result = await original.apply(this, arguments);
    await appendSnapshotInsight();
    return result;
  });

  document.addEventListener('DOMContentLoaded', () => {
    window.setTimeout(() => {
      const overlay = document.getElementById('login-overlay');
      if (overlay && overlay.style.display === 'flex') {
        return;
      }
      if (typeof window.navigate === 'function') {
        window.navigate('observability-summary');
      }
    }, 1400);
  });

  applySidebarOrder();
})();

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
    const data = await window.api('/api/observability/snapshot');
    if (!data || window.isApiError?.(data) || !data.summary) {
      return;
    }
    const snapshot = data.snapshot || {};
    const summary = data.summary || {};
    const activeTab = document.querySelector('#page-observability .filter-btn.active');
    const currentTarget = activeTab?.id?.replace('obs-tab-', '') || 'grafana';
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
    panel.insertAdjacentHTML(
      'beforeend',
      `
        <div id="obs-shell-insight" style="margin-top:12px;padding:10px;border-radius:10px;border:1px solid ${accent}33;background:rgba(8,16,30,0.6);line-height:1.6">
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.7px;color:var(--muted)">Smart Context</div>
          <div style="margin-top:5px;color:#e5eefc;font-weight:600">${escText(summary.headline || 'No summary available.')}</div>
          <div style="margin-top:6px;color:#b8c9e0">Current view: ${escText(currentTarget)} is ${escText(targetState.status || 'unknown')}.</div>
          <div style="margin-top:4px;color:#b8c9e0">Recommended Kuma state: ${escText(summary.recommended_kuma_state || 'up')}.</div>
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

  applySidebarOrder();
})();

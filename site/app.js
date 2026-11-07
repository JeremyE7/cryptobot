const $ = id => document.getElementById(id);
const money = n => `$${Number(n || 0).toFixed(2)}`;
const pct = n => `${Number(n || 0) >= 0 ? '+' : ''}${Number(n || 0).toFixed(2)}%`;
const cls = n => Number(n || 0) < 0 ? 'neg' : 'pos';

function path(points, key, w, h) {
  if (points.length < 2) return '';
  const values = points.map(x => x[key]);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const range = Math.max(hi - lo, .0001);
  const pad = 8;
  return points
    .map((x, i) => `${i ? 'L' : 'M'}${(pad + i * (w - 2 * pad) / (points.length - 1)).toFixed(1)},${(pad + (hi - x[key]) * (h - 2 * pad) / range).toFixed(1)}`)
    .join(' ');
}

function equityChart(points) {
  if (points.length < 2) return;
  const d = path(points, 'equity', 900, 300);
  $('linePath').setAttribute('d', d);
  $('areaPath').setAttribute('d', `${d} L892,312 L8,312 Z`);
  $('equityEmpty').hidden = true;
}

function derived(points) {
  let peak = 0;
  let prev = null;
  return points.map(p => {
    peak = Math.max(peak, p.equity);
    const dd = peak ? 100 * (p.equity / peak - 1) : 0;
    const ret = prev ? 100 * (p.equity / prev - 1) : 0;
    prev = p.equity;
    return { ...p, dd, ret };
  });
}

function drawdown(points) {
  if (points.length < 2) return;
  const d = derived(points);
  $('ddPath').setAttribute('d', path(d, 'dd', 600, 170));
  $('ddEmpty').hidden = true;
}

function daily(points) {
  const d = derived(points).slice(-40).filter((_, i) => i > 0);
  const box = $('dailyBars');
  if (!d.length) return;
  if (d.every(x => Math.abs(x.ret) < 1e-3)) {
    box.innerHTML = '<div class="empty-inline">Sin cambios: el bot estuvo en efectivo.</div>';
    return;
  }
  const max = Math.max(...d.map(x => Math.abs(x.ret)), .01);
  box.innerHTML = d.map(x =>
    `<i class="bar ${x.ret < 0 ? 'neg' : ''}" style="--h:${Math.max(3, Math.abs(x.ret) / max * 85)}%" title="${x.time}: ${pct(x.ret)}"></i>`
  ).join('');
}

function actionClass(kind) {
  return ['buy', 'sell', 'cash', 'hold', 'waiting', 'rebalance', 'invested'].includes(kind) ? kind : 'cash';
}

function renderPositions(d) {
  const pos = d.positions || [];
  const cashWeight = d.equity ? d.cash / d.equity * 100 : 100;
  $('positions').innerHTML = [
    ...pos.map(p => `<div class="position-row"><span>${p.symbol}</span><span>${money(p.value)} · ${Number(p.weight_pct || 0).toFixed(1)}%</span></div>`),
    `<div class="position-row cash-row"><span>EFECTIVO</span><span>${cashWeight.toFixed(1)}%</span></div>`
  ].join('');
}

function renderTrades(d) {
  const trades = d.recent_trades || [];
  const buys = trades.filter(t => t.side === 'BUY').length;
  const sells = trades.filter(t => t.side === 'SELL').length;
  $('tradeSummary').textContent = trades.length
    ? `${buys} compra${buys === 1 ? '' : 's'} · ${sells} venta${sells === 1 ? '' : 's'}`
    : 'sin operaciones todavía';

  if (!trades.length) {
    $('tradeTable').innerHTML = '<p class="muted">Todavía no hay compras ni ventas. Cuando el bot ejecute una orden, aparecerá aquí con fecha, activo y monto.</p>';
    return;
  }

  $('tradeTable').innerHTML = `
    <div class="trade-row trade-head">
      <span>Fecha</span><span>Acción</span><span>Activo</span><span>Monto</span><span>Precio</span>
    </div>
    ${trades.slice(0, 40).map(t => `
      <div class="trade-row">
        <span class="trade-date">${t.date}</span>
        <span class="action-pill ${t.side === 'BUY' ? 'buy' : 'sell'}">${t.action}</span>
        <strong>${t.symbol}</strong>
        <span>${money(t.notional)}</span>
        <span class="muted">${money(t.price)}</span>
      </div>`).join('')}
  `;
}

function renderDecisions(d) {
  const decisions = d.decisions || [];
  if (!decisions.length) {
    $('ledger').innerHTML = '<p class="muted">Todavía no hay decisiones procesadas.</p>';
    return;
  }
  $('ledger').innerHTML = decisions.map(x => `
    <div class="decision-row" title="${x.reason}">
      <span class="decision-date">${x.date}</span>
      <span class="action-pill ${actionClass(x.action_kind)}">${x.action}</span>
      <div class="decision-copy">
        <strong>${x.summary}</strong>
        <small>${x.trigger_label} · régimen ${x.regime} · objetivo ${Number(x.target_exposure_pct || 0).toFixed(0)}%</small>
      </div>
    </div>`).join('');
}

function renderBenchmarks(d) {
  const marks = d.benchmarks || [];
  const labels = {
    BTCUSDT: 'Bitcoin',
    BNBUSDT: 'BNB',
    ETHUSDT: 'Ethereum',
    LINKUSDT: 'Chainlink',
    EQUAL_WEIGHT: 'Cartera igualada'
  };
  if (!marks.length) {
    $('benchmarks').innerHTML = '<p class="muted">Sin datos de benchmark todavía.</p>';
    return;
  }
  $('benchmarks').innerHTML = marks.map(b => `
    <div class="benchmark-card">
      <span>${labels[b.name] || b.name}</span>
      <strong class="${cls(b.return_pct)}">${pct(b.return_pct)}</strong>
    </div>`).join('');
}

function render(d) {
  const equity = Number(d.equity || 0);
  const invested = Number(d.invested || 0);
  const exp = equity ? invested / equity * 100 : 0;
  const current = d.current || {};
  const regime = current.regime || d.regime || 'NEUTRAL';
  const regimeLabel = current.regime_label || d.regime_meta?.label || regime;

  $('botAction').textContent = current.action || (invested > 0 ? 'INVERTIDO' : 'EN EFECTIVO');
  $('botAction').className = `action-badge ${actionClass(current.kind)}`;
  $('regimeBadge').textContent = regimeLabel;
  $('regimeBadge').className = `regime-badge ${String(regime).toLowerCase()}`;
  $('botHeadline').textContent = current.headline || 'Estado del bot';
  $('botExplain').textContent = current.explanation || '';
  $('heroExposure').textContent = `${exp.toFixed(1)}%`;
  $('heroCash').textContent = money(d.cash);
  $('heroRegime').textContent = regimeLabel;

  const lastTrade = (d.recent_trades || [])[0];
  if (lastTrade) {
    $('lastAction').textContent = `${lastTrade.action} ${lastTrade.symbol}`;
    $('lastActionDetail').textContent = `${lastTrade.date} · ${money(lastTrade.notional)}`;
  } else {
    $('lastAction').textContent = 'Sin operaciones';
    $('lastActionDetail').textContent = 'El bot todavía no ha comprado ni vendido.';
  }
  $('nextCheck').textContent = current.next_check || 'Cada día a las 00:17 UTC';

  $('equity').textContent = equity.toFixed(2);
  $('returnValue').textContent = pct(d.return_pct);
  $('returnValue').style.color = Number(d.return_pct) < 0 ? 'var(--red)' : 'var(--green)';
  $('exposure').textContent = `${exp.toFixed(1)}%`;
  $('invested').textContent = `${money(invested)} invertido`;
  $('cash').textContent = money(d.cash);
  $('drawdown').textContent = pct(d.max_drawdown_pct);
  $('drawdown').style.color = Number(d.max_drawdown_pct) < 0 ? 'var(--red)' : 'var(--ink)';
  $('regime').textContent = regimeLabel;
  $('regimeShort').textContent = current.regime_exposure ? `objetivo ${current.regime_exposure}` : 'estado actual';
  $('trades').textContent = d.trades ?? 0;
  $('costs').textContent = `${money(d.costs)} en costos`;
  $('integrityLabel').textContent = d.integrity ? '● ledger íntegro' : '● revisar integridad';
  $('lastUpdate').textContent = `Última actualización · ${d.updated_at || '—'} · auto-refresh 60s`;
  $('startedAt').textContent = `inicio · ${d.started_at || '—'}`;
  $('strategyLabel').textContent = `${d.strategy_version || d.strategy || 'paper'} · paper only`;
  $('donutExposure').textContent = `${exp.toFixed(0)}%`;
  $('allocationDonut').style.setProperty('--exposure', `${Math.max(0, Math.min(100, exp)) * 3.6}deg`);

  document.querySelectorAll('.regime-card').forEach(card => {
    card.classList.toggle('active', card.dataset.regime === regime);
  });

  renderPositions(d);
  renderTrades(d);
  renderDecisions(d);
  renderBenchmarks(d);

  const pts = (d.equity_curve || []).filter(p => Number.isFinite(Number(p.equity)));
  equityChart(pts);
  drawdown(pts);
  daily(pts);
}

async function refreshDashboard() {
  try {
    const response = await fetch(`./data.json?t=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(response.status);
    render(await response.json());
  } catch {
    $('integrityLabel').textContent = '● sin datos';
    $('botAction').textContent = 'SIN DATOS';
    $('botAction').className = 'action-badge cash';
    $('botHeadline').textContent = 'No se pudo cargar el estado del bot';
    $('botExplain').textContent = 'El dashboard se actualiza cuando el workflow diario publica data.json.';
  }
}

refreshDashboard();
setInterval(refreshDashboard, 60_000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshDashboard();
});

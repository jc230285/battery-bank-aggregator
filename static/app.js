(function () {
  "use strict";
  const D = window.BBA_DATA || {};
  let products = D.products || [];
  let _lastProductsModified = null;

  // Scraped strings are untrusted — escape before innerHTML; only allow http(s) URLs.
  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function safeUrl(u) { return /^https?:\/\//i.test(u || "") ? esc(u) : "#"; }

  // ---- categories ----
  const CATEGORIES = [["power_bank", "Power Banks"], ["power_station", "Power Stations"], ["watchlist", "Watchlist"]];

  // Value-to-you factors per category: [key, label, value(p), invert(lower-is-better)].
  const FACTORS_BY_CAT = {
    power_bank: [
      ["cost", "Cheap (cost per mAh)", p => p.cost_per_mah, true],
      ["capacity", "Capacity (mAh)", p => p.claimed_mah, false],
      ["pd_w", "Fast charge (W)", p => (p.pd_w || p.max_w), false],
      ["usb_c", "USB-C ports", p => p.usb_c, false],
      ["wireless", "Wireless", p => (p.wireless ? 1 : 0), false],
      ["display", "Display", p => (p.display ? 1 : 0), false],
      ["passthrough", "Pass-through", p => (p.passthrough ? 1 : 0), false],
      ["solar", "Solar", p => (p.solar ? 1 : 0), false],
      ["rating", "High rating", p => p.rating, false],
      ["honesty", "Honesty", p => honestyScore(p), false],
    ],
    power_station: [
      ["cost", "Cheap (cost per Wh)", p => p.cost_per_wh, true],
      ["capacity", "Capacity (Wh)", p => p.capacity_wh, false],
      ["ac_output_w", "AC output (W)", p => p.ac_output_w, false],
      ["ac_sockets", "AC sockets", p => p.ac_sockets, false],
      ["solar_input_w", "Solar input (W)", p => p.solar_input_w, false],
      ["pd_w", "USB-C PD (W)", p => p.pd_w, false],
      ["cycle_life", "Cycle life", p => p.cycle_life, false],
      ["expandable", "Expandable", p => (p.expandable ? 1 : 0), false],
      ["ups", "UPS", p => (p.ups ? 1 : 0), false],
      ["rating", "High rating", p => p.rating, false],
      ["honesty", "Honesty", p => honestyScore(p), false],
    ],
    watchlist: [
      ["rating", "High rating", p => p.rating, false],
      ["honesty", "Honesty", p => honestyScore(p), false],
    ],
  };
  function factors() { return FACTORS_BY_CAT[state.category] || FACTORS_BY_CAT.power_bank; }

  const HONESTY_KEYS = [
    ["physics", "Physics (weight vs capacity)"],
    ["price", "Price-per-capacity outlier"],
    ["brand", "Brand trust"],
    ["reviews", "Review text"],
    ["consistency", "mAh/Wh self-consistency"],
  ];

  // ---- filters (declarative, per category) ----
  const COMMON_FILTERS = [
    { key: "brand", label: "Brand contains", type: "text",
      test: (p, v) => !v || (p.brand || "").toLowerCase().includes(v.toLowerCase()) },
    { key: "chemistry", label: "Chemistry", type: "select",
      options: [["", "Any"], ["lifepo4", "LiFePO4"], ["li-ion", "Li-ion"],
                ["li-po", "Li-polymer"], ["nmc", "NMC"], ["unknown", "Unknown"]],
      test: (p, v) => !v || (v === "unknown" ? !p.chemistry : p.chemistry === v) },
    { key: "maxPrice", label: "Max price (£)", type: "num",
      test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.price != null && p.price <= v); } },
    { key: "rating", label: "Min rating", type: "num",
      test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.rating || 0) >= v; } },
    { key: "minReviews", label: "Min ratings (count)", type: "num",
      test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.review_count || 0) >= v; } },
    { key: "honesty", label: "Min honesty", type: "range",
      test: (p, v) => { v = parseFloat(v) || 0; if (!v) return true; const s = honestyScore(p); return s != null && s >= v; } },
    { key: "stock", label: "In stock only", type: "check", test: (p, v) => !v || p.in_stock },
    // Delisted = absent from Amazon results for >REMOVE_AFTER_HOURS or 404 on
    // refresh. Hidden by default so the live catalog doesn't drift; tick to
    // include them (their price history is preserved either way).
    { key: "showDelisted", label: "Show delisted", type: "check", test: (p, v) => v || !p.delisted_at },
  ];
  const FILTERS_BY_CAT = {
    power_bank: COMMON_FILTERS.concat([
      { key: "mah", label: "Min mAh", type: "num",
        test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.claimed_mah || 0) >= v; } },
      { key: "mahMax", label: "Max mAh", type: "num",
        test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.claimed_mah || 0) <= v; } },
      { key: "usbc", label: "Has USB-C", type: "check", test: (p, v) => !v || p.usb_c > 0 },
      { key: "wireless", label: "Wireless", type: "check", test: (p, v) => !v || p.wireless },
      { key: "display", label: "Display", type: "check", test: (p, v) => !v || p.display },
      { key: "passthrough", label: "Pass-through", type: "check", test: (p, v) => !v || p.passthrough },
      { key: "solar", label: "Solar", type: "check", test: (p, v) => !v || p.solar },
    ]),
    power_station: COMMON_FILTERS.concat([
      { key: "wh", label: "Min Wh", type: "num",
        test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.capacity_wh || 0) >= v; } },
      { key: "whMax", label: "Max Wh", type: "num",
        test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.capacity_wh || 0) <= v; } },
      { key: "acw", label: "Min AC output (W)", type: "num",
        test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.ac_output_w || 0) >= v; } },
      { key: "sockets", label: "Min AC sockets", type: "num",
        test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.ac_sockets || 0) >= v; } },
      { key: "solarin", label: "Solar input", type: "check",
        test: (p, v) => !v || p.solar || p.solar_input_w },
      { key: "expandable", label: "Expandable", type: "check", test: (p, v) => !v || p.expandable },
      { key: "ups", label: "UPS", type: "check", test: (p, v) => !v || p.ups },
    ]),
    // Watchlist items are arbitrary; the spec-specific filters don't apply.
    watchlist: [
      { key: "brand", label: "Brand contains", type: "text",
        test: (p, v) => !v || (p.brand || "").toLowerCase().includes(v.toLowerCase()) },
      { key: "rating", label: "Min rating", type: "num",
        test: (p, v) => { v = parseFloat(v) || 0; return !v || (p.rating || 0) >= v; } },
      { key: "stock", label: "In stock only", type: "check", test: (p, v) => !v || p.in_stock },
      { key: "showDelisted", label: "Show delisted", type: "check", test: (p, v) => v || !p.delisted_at },
    ],
  };
  function filterSpecs() { return FILTERS_BY_CAT[state.category] || FILTERS_BY_CAT.power_bank; }

  const LS_KEY = "bba_ui_v4";

  // ---- persisted state ----
  const defaults = {
    category: "power_bank",
    filtersByCat: { power_bank: { honesty: 0 }, power_station: { honesty: 0 }, watchlist: {} },
    honestyWeights: Object.assign(
      { physics: 0.35, price: 0.20, brand: 0.2, reviews: 0.15, consistency: 0.10 }, D.honesty_weights || {}),
    weightsByCat: {
      power_bank: Object.fromEntries(FACTORS_BY_CAT.power_bank.map(([k]) => [k, 0])),
      power_station: Object.fromEntries(FACTORS_BY_CAT.power_station.map(([k]) => [k, 0])),
      watchlist: Object.fromEntries(FACTORS_BY_CAT.watchlist.map(([k]) => [k, 0])),
    },
    sortKey: "rating", sortDir: -1,
  };
  function loadState() {
    const base = JSON.parse(JSON.stringify(defaults));
    try {
      const s = JSON.parse(localStorage.getItem(LS_KEY)) || {};
      if (s.category) base.category = s.category;
      Object.assign(base.honestyWeights, s.honestyWeights || {});
      CATEGORIES.forEach(([c]) => {
        if (s.filtersByCat && base.filtersByCat[c]) Object.assign(base.filtersByCat[c], s.filtersByCat[c] || {});
        if (s.weightsByCat && base.weightsByCat[c]) Object.assign(base.weightsByCat[c], s.weightsByCat[c] || {});
      });
      if (s.sortKey) base.sortKey = s.sortKey;
      if (s.sortDir) base.sortDir = s.sortDir;
    } catch (e) { /* ignore corrupt state */ }
    return base;
  }
  const state = loadState();
  function saveState() { localStorage.setItem(LS_KEY, JSON.stringify(state)); }
  function curFilters() { return state.filtersByCat[state.category]; }
  function curWeights() { return state.weightsByCat[state.category]; }

  function ageAgo(isoStr) {
    if (!isoStr) return "";
    const hrs = (Date.now() - new Date(isoStr)) / 3600000;
    if (hrs < 1) return `${Math.round(hrs * 60)}m ago`;
    if (hrs < 24) return `${Math.round(hrs)}h ago`;
    return `${Math.round(hrs / 24)}d ago`;
  }

  // ---- derived metrics ----
  function honestyScore(p) {
    const h = p.honesty || {}, w = state.honestyWeights;
    let num = 0, den = 0;
    HONESTY_KEYS.forEach(([k]) => { if (h[k] != null) { num += h[k] * w[k]; den += w[k]; } });
    return den > 0 ? (num / den) * 100 : null;
  }
  let factorRange = {};
  function computeFactorRanges(rows) {
    factorRange = {};
    factors().forEach(([k, , acc]) => {
      let mn = Infinity, mx = -Infinity;
      rows.forEach(p => {
        const v = acc(p);
        if (v != null && isFinite(v)) { if (v < mn) mn = v; if (v > mx) mx = v; }
      });
      factorRange[k] = mn === Infinity ? null : { min: mn, max: mx };
    });
  }
  function factorNorm(p, k, acc, invert) {
    const r = factorRange[k];
    if (!r) return 0;
    const v = acc(p);
    if (v == null || !isFinite(v)) return 0;
    if (r.max === r.min) return r.max > 0 ? 1 : 0;
    const n = (v - r.min) / (r.max - r.min);
    return invert ? 1 - n : n;
  }
  function valueToYou(p) {
    const w = curWeights();
    let num = 0, den = 0;
    factors().forEach(([k, , acc, invert]) => {
      const wt = w[k] || 0;
      if (wt > 0) { num += wt * factorNorm(p, k, acc, invert); den += wt; }
    });
    return den > 0 ? (num / den) * 100 : null;
  }
  function costPer10k(p) { return p.cost_per_mah != null ? p.cost_per_mah * 10000 : null; }

  // ---- filtering / sorting ----
  function passesFilters(p) {
    if ((p.category || "power_bank") !== state.category) return false;
    const f = curFilters();
    return filterSpecs().every(spec => spec.test(p, f[spec.key]));
  }
  const SORT_ACCESSORS = {
    title: p => (p.title || "").toLowerCase(), price: p => p.price, avg: p => p.avg_price,
    claimed_mah: p => p.claimed_mah, cost_per_mah: p => p.cost_per_mah,
    capacity_wh: p => p.capacity_wh, cost_per_wh: p => p.cost_per_wh,
    ac_output_w: p => p.ac_output_w, rating: p => p.rating, reviews: p => p.review_count,
    honesty: p => honestyScore(p), value: p => valueToYou(p), price_delta: p => p.price_delta,
    all_time_low: p => p.all_time_low,
  };
  function cmp(a, b) {
    const f = SORT_ACCESSORS[state.sortKey] || (() => 0);
    let va = f(a), vb = f(b);
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "string") return va.localeCompare(vb) * state.sortDir;
    return (va - vb) * state.sortDir;
  }

  // ---- cell renderers ----
  function sparkline(p) {
    const h = (p.history || []).filter(x => x.price != null);
    if (h.length < 2) return `<span class="text-slate-600" title="no price history yet">—</span>`;
    const ps = h.map(x => x.price);
    const min = Math.min(...ps), max = Math.max(...ps), rng = (max - min) || 1;
    const w = 70, ht = 20;
    const pts = ps.map((v, i) => `${(i / (ps.length - 1)) * w},${ht - ((v - min) / rng) * ht}`).join(" ");
    const up = ps[ps.length - 1] >= ps[0];
    const pct = ps[0] ? ((ps[ps.length - 1] - ps[0]) / ps[0]) * 100 : 0;
    const sign = pct > 0 ? "+" : "";
    const colour = up ? "text-red-400" : "text-emerald-400";
    const delta = Math.abs(pct) >= 1
      ? `<span class="${colour} text-[10px] ml-1" title="change since first recorded price">${sign}${pct.toFixed(0)}%</span>`
      : "";
    const t0 = new Date(h[0].t).toLocaleDateString("en-GB");
    const t1 = new Date(h[h.length - 1].t).toLocaleDateString("en-GB");
    const tip = `${h.length} price points · £${min.toFixed(2)}–£${max.toFixed(2)} · ${t0} → ${t1}`;
    return `<span class="inline-flex items-center"><svg width="${w}" height="${ht}" title="${esc(tip)}" class="inline-block align-middle"><polyline fill="none" stroke="${up ? '#f87171' : '#34d399'}" stroke-width="1.5" points="${pts}"/></svg>${delta}</span>`;
  }

  // Percentile-based Deal cutoff (set per render). The regression's absolute
  // calibration is noisy, but RELATIVE under-pricing among the filtered list is
  // a useful signal — we badge the deepest 15% of price-vs-fair ratios.
  let _dealCutoff = -0.25;
  function computeDealCutoff(rows) {
    const ratios = rows
      .filter(p => p.fair_price > 0 && p.price_delta != null)
      .map(p => p.price_delta / p.fair_price)
      .sort((a, b) => a - b);
    _dealCutoff = ratios.length ? Math.min(-0.10, ratios[Math.floor(ratios.length * 0.15)])
                                : -0.25;  // never badge merely-on-fair products
  }
  function dealBadge(p) {
    if (p.price == null || p.fair_price == null || p.price_delta == null) return "";
    if (p.fair_price <= 0) return "";
    const r = p.price_delta / p.fair_price;
    if (r > _dealCutoff) return "";
    const pct = r * 100;
    const tip = `Price £${p.price.toFixed(2)} vs fair £${p.fair_price.toFixed(2)} (${pct.toFixed(0)}%) — among top deals in the filtered list`;
    return `<span class="badge bg-emerald-700 text-emerald-100" title="${tip}">Deal ${pct.toFixed(0)}%</span> `;
  }
  function _featureCat(p) {
    if (p.category === "power_station") return "power_station";
    if (p.category !== "watchlist") return "power_bank";
    // Watchlist: infer from available data (mirrors analysis._analysis_cat logic)
    if (p.capacity_wh && !p.claimed_mah) return "power_station";
    if (p.ac_output_w) return "power_station";
    return "power_bank";
  }
  function featureIcons(p) {
    const out = [];
    if (_featureCat(p) === "power_station") {
      if (p.ac_output_w) out.push(`${p.ac_output_w}W AC`);
      if (p.ac_sockets) out.push(`${p.ac_sockets}×AC`);
      if (p.solar_input_w) out.push(`${p.solar_input_w}W solar`);
      if (p.usb_c) out.push(`C×${p.usb_c}`);
      if (p.pd_w) out.push(`${p.pd_w}W PD`);
      if (p.cycle_life) out.push(`${p.cycle_life} cyc`);
      if (p.expandable) out.push("Expand");
      if (p.ups) out.push("UPS");
      if (p.weight_g) out.push(`${(p.weight_g / 1000).toFixed(1)}kg`);
    } else {
      if (p.usb_c) out.push(`C×${p.usb_c}`);
      if (p.usb_a) out.push(`A×${p.usb_a}`);
      if (p.pd_w) out.push(`${p.pd_w}W PD`);
      if (p.wireless) out.push("Qi");
      if (p.display) out.push("Disp");
      if (p.passthrough) out.push("PT");
      if (p.solar) out.push("Solar");
      if (p.weight_g) out.push(`${Math.round(p.weight_g)}g`);
    }
    return out.map(t => `<span class="badge bg-slate-700 text-slate-200">${esc(t)}</span>`).join(" ");
  }
  const _CRITICAL_FLAGS = new Set(["impossible_capacity", "reviews_report_fake_capacity"]);
  const _WARN_FLAGS = new Set(["too_cheap_per_capacity", "inconsistent_capacity_claims",
                               "unverified_brand", "unknown_brand", "brand_low_reputation"]);
  const _FLAG_LABELS = {
    impossible_capacity:          "Capacity overstated",
    reviews_report_fake_capacity: "Reviewers report fake capacity",
    too_cheap_per_capacity:       "Suspiciously cheap per capacity",
    inconsistent_capacity_claims: "mAh/Wh figures don't match",
    unverified_brand:             "Unverified brand",
    unknown_brand:                "Unknown brand",
    brand_low_reputation:         "Low brand reputation",
  };
  function _flagLabel(f) {
    if (_FLAG_LABELS[f]) return _FLAG_LABELS[f];
    if (f.startsWith("brand_mimics_")) { const b = f.slice(13).replace(/_/g, " "); return `Mimics ${b.charAt(0).toUpperCase() + b.slice(1)}`; }
    return f.replace(/_/g, " ");
  }
  function honestyFlagBubbles(p) {
    return (p.honesty_flags || []).map(f => {
      const critical = _CRITICAL_FLAGS.has(f) || f.startsWith("brand_mimics_");
      const warn = _WARN_FLAGS.has(f);
      const cls = critical ? "bg-red-900 text-red-200"
                : warn     ? "bg-amber-900 text-amber-200"
                :            "bg-slate-700 text-slate-400";
      return `<span class="badge ${cls}" title="${f}">${_flagLabel(f)}</span>`;
    }).join(" ");
  }
  function valueBreakdown(p) {
    const w = curWeights();
    const parts = factors().filter(([k]) => (w[k] || 0) > 0)
      .map(([k, label, acc, invert]) => `${label}: ${Math.round(factorNorm(p, k, acc, invert) * 100)}/100 × weight ${w[k]}`);
    return parts.length
      ? "Fit score = weighted average of (relative to the filtered list):\n" + parts.join("\n")
      : "Raise a 'Features I want' slider to score this product.";
  }
  function priceCell(p) {
    let cls = "text-slate-200";
    if (p.price != null && p.avg_price != null) cls = p.price < p.avg_price ? "text-emerald-400" : p.price > p.avg_price ? "text-red-400" : cls;
    const main = p.price != null ? `<span class="${cls}">£${p.price.toFixed(2)}</span>` : "—";
    const avg = p.avg_price != null ? `<div class="text-[10px] text-slate-500">avg £${p.avg_price.toFixed(2)}</div>` : "";
    const atl = (p.all_time_low != null && p.price != null && p.price <= p.all_time_low * 1.03)
      ? `<span class="inline-block text-[9px] font-semibold bg-sky-900 text-sky-300 rounded px-1 ml-1" title="At or near all-time low (£${p.all_time_low.toFixed(2)})">ATL</span>`
      : "";
    return `<td class="px-2 py-2 whitespace-nowrap">${main}${atl}${avg}</td>`;
  }
  const CELL = {
    img: p => `<td class="px-2 py-2">${p.image_url ? `<img src="${safeUrl(p.image_url)}" class="w-10 h-10 object-contain">` : ""}</td>`,
    product: p => {
      const rm = p.category === "watchlist"
        ? ` <button class="watchlist-rm text-red-400 hover:text-red-300 text-xs ml-1" data-asin="${esc(p.asin)}" data-title="${esc(p.title || p.asin)}" title="Remove from watchlist">✕</button>`
        : "";
      const age = ageAgo(p.last_seen);
      const ageHtml = age ? `<span class="text-[9px] text-slate-600 ml-1" title="Last seen: ${esc(p.last_seen || '')}">${esc(age)}</span>` : "";
      return `<td class="px-2 py-2 max-w-sm"><a href="${safeUrl(p.url)}" target="_blank" rel="noopener noreferrer" class="text-sky-300 hover:underline line-clamp-2">${esc(p.title || p.asin)}</a>${rm}<div class="text-xs text-slate-500">${esc(p.brand || "?")}${p.chemistry ? " · " + esc(p.chemistry) : ""}${ageHtml}</div></td>`;
    },
    price: priceCell,
    mah: p => {
      if (!p.claimed_mah) return `<td class="px-2 py-2">—</td>`;
      const h = p.honesty || {};
      let tip = "";
      if (h.mah_cap) {
        tip = `Max plausible from weight: ~${h.mah_cap.toLocaleString()}mAh`;
        if (h.overstatement_pct) tip += ` — overstated by ~${h.overstatement_pct}%`;
      }
      return `<td class="px-2 py-2"${tip ? ` title="${esc(tip)}"` : ""}>${p.claimed_mah.toLocaleString()}</td>`;
    },
    cost10k: p => {
      const c = costPer10k(p);
      if (c == null) return `<td class="px-2 py-2 whitespace-nowrap">—</td>`;
      const fake = (p.honesty_flags || []).includes("impossible_capacity");
      const cls = fake ? "line-through text-slate-500" : "";
      const tip = fake ? "Based on overstated mAh — the real £/10Ah is higher" : "";
      return `<td class="px-2 py-2 whitespace-nowrap"><span class="${cls}" title="${tip}">£${c.toFixed(2)}</span></td>`;
    },
    wh: p => {
      if (!p.capacity_wh) return `<td class="px-2 py-2 whitespace-nowrap">—</td>`;
      const h = p.honesty || {};
      let tip = "";
      if (h.wh_cap) {
        tip = `Max plausible from weight: ~${Math.round(h.wh_cap).toLocaleString()}Wh`;
        if (h.overstatement_pct) tip += ` — overstated by ~${h.overstatement_pct}%`;
      }
      return `<td class="px-2 py-2 whitespace-nowrap"${tip ? ` title="${esc(tip)}"` : ""}>${Math.round(p.capacity_wh).toLocaleString()}Wh</td>`;
    },
    costwh: p => {
      if (p.cost_per_wh == null) return `<td class="px-2 py-2 whitespace-nowrap">—</td>`;
      const fake = (p.honesty_flags || []).includes("impossible_capacity");
      const cls = fake ? "line-through text-slate-500" : "";
      const tip = fake ? "Based on overstated Wh — the real £/Wh is higher" : "";
      return `<td class="px-2 py-2 whitespace-nowrap"><span class="${cls}" title="${tip}">£${p.cost_per_wh.toFixed(2)}</span></td>`;
    },
    acw: p => `<td class="px-2 py-2 whitespace-nowrap">${p.ac_output_w ? p.ac_output_w + "W" : "—"}</td>`,
    // Feature tags + compact honesty score + flag bubbles.
    features: p => {
      const f = honestyFlagBubbles(p);
      const delisted = p.delisted_at
        ? `<span class="badge bg-slate-700 text-amber-300" title="Delisted at ${p.delisted_at}">delisted</span> ` : "";
      const hs = honestyScore(p);
      const hsCls = hs == null ? "" : hs >= 75 ? "text-emerald-400" : hs >= 50 ? "text-amber-400" : "text-red-400";
      const hsBadge = hs != null
        ? `<span class="text-[10px] ${hsCls} mr-1" title="Honesty score: ${hs.toFixed(0)}/100">H:${hs.toFixed(0)}</span>`
        : "";
      return `<td class="px-2 py-2">${delisted}${dealBadge(p)}${featureIcons(p)}${hsBadge}${f ? " " + f : ""}</td>`;
    },
    rating: p => {
      const stars = p.rating != null ? p.rating.toFixed(1) + "★" : "—";
      const cnt = Number.isFinite(p.review_count) ? `<div class="text-[10px] text-slate-500">${p.review_count.toLocaleString()} ratings</div>` : "";
      return `<td class="px-2 py-2 whitespace-nowrap">${stars}${cnt}</td>`;
    },
    value: p => { const v = valueToYou(p); return `<td class="px-2 py-2 whitespace-nowrap"><span title="${valueBreakdown(p)}">${v != null ? Math.round(v) : `<span class="text-slate-600">—</span>`}</span></td>`; },
    trend: p => `<td class="px-2 py-2 whitespace-nowrap">${sparkline(p)}</td>`,
    // Watchlist capacity: mAh for power banks, Wh for power stations, — when unknown.
    wl_cap: p => {
      if (p.claimed_mah) return `<td class="px-2 py-2 whitespace-nowrap">${p.claimed_mah.toLocaleString()}mAh</td>`;
      if (p.capacity_wh) return `<td class="px-2 py-2 whitespace-nowrap">${Math.round(p.capacity_wh).toLocaleString()}Wh</td>`;
      return `<td class="px-2 py-2 whitespace-nowrap text-slate-600">—</td>`;
    },
  };
  const HEAD = [["", null, "img"], ["Product", "title", "product"], ["Price", "price", "price"]];
  const TAIL = [["Features", null, "features"], ["Rating", "rating", "rating"],
    ["Value", "value", "value"], ["Trend", null, "trend"]];
  const COLS_BY_CAT = {
    power_bank: HEAD.concat([["mAh", "claimed_mah", "mah"], ["£/10Ah", "cost_per_mah", "cost10k"]], TAIL),
    power_station: HEAD.concat([["Wh", "capacity_wh", "wh"], ["£/Wh", "cost_per_wh", "costwh"], ["AC W", "ac_output_w", "acw"]], TAIL),
    watchlist: HEAD.concat([["Capacity", null, "wl_cap"], ["Features", null, "features"], ["Rating", "rating", "rating"], ["Trend", null, "trend"]]),
  };
  function cols() { return COLS_BY_CAT[state.category] || COLS_BY_CAT.power_bank; }

  // ---- rendering ----
  function renderHead() {
    document.getElementById("head-row").innerHTML = cols().map(([label, key]) => {
      if (!key) return `<th class="text-left py-2 px-2 font-medium">${label}</th>`;
      const arrow = state.sortKey === key ? (state.sortDir === 1 ? " ▲" : " ▼") : "";
      return `<th class="text-left py-2 px-2 font-medium sortable" data-key="${key}">${label}${arrow}</th>`;
    }).join("");
    document.querySelectorAll("th.sortable").forEach(th => th.onclick = () => {
      const k = th.dataset.key;
      if (state.sortKey === k) state.sortDir *= -1; else { state.sortKey = k; state.sortDir = 1; }
      saveState(); render();
    });
  }
  function renderModelNote() {
    const el = document.getElementById("model-note");
    if (!el) return;
    const w = curWeights();
    const total = factors().reduce((a, [k]) => a + (w[k] || 0), 0);
    if (total === 0) {
      el.innerHTML = "💡 <b>Value</b> is a 0–100 fit score — raise the “Features I want” sliders " +
        "(incl. <b>Cheap</b>) for what you care about; it scores each product against the filtered list.";
    } else {
      el.textContent = "Value = 0–100 fit score (vs filtered list), weighted by: " +
        factors().filter(([k]) => (w[k] || 0) > 0).map(([, l]) => l).join(", ");
    }
  }
  function priceHistogramSvg(vals, scale) {
    if (vals.length < 5) return "";
    const sorted = [...vals].sort((a, b) => a - b);
    const lo = sorted[0];
    const hi = sorted[Math.floor(sorted.length * 0.95)];  // clip top 5% so a few outliers don't flatten the scale
    if (hi <= lo) return "";
    const bins = 14;
    const counts = Array(bins).fill(0);
    sorted.forEach(v => {
      const x = v > hi ? hi : v;
      const i = Math.min(bins - 1, Math.floor((x - lo) / (hi - lo) * bins));
      counts[i]++;
    });
    const maxC = Math.max(...counts) || 1;
    const w = 160, h = 26, bw = w / bins;
    const bars = counts.map((c, i) =>
      `<rect x="${i * bw}" y="${h - (c / maxC) * h}" width="${bw - 1}" height="${(c / maxC) * h}" fill="#0ea5e9"/>`
    ).join("");
    const tip = `Distribution: £${(lo * scale).toFixed(2)} – £${(hi * scale).toFixed(2)} (top 5% clipped)`;
    return `<svg width="${w}" height="${h}" title="${tip}" class="inline-block align-middle">${bars}</svg>`;
  }

  function renderSummaryBar(rows) {
    const el = document.getElementById("summary-bar");
    if (!el) return;
    const inCat = products.filter(p => (p.category || "power_bank") === state.category);
    const metricKey = state.category === "power_station" ? "cost_per_wh" : "cost_per_mah";
    const metricLabel = state.category === "power_station" ? "£/Wh" : "£/10Ah";
    const scale = state.category === "power_station" ? 1 : 10000;  // £/10Ah for banks
    const vals = rows.map(p => p[metricKey]).filter(v => typeof v === "number" && v > 0).sort((a, b) => a - b);
    const median = vals.length ? vals[Math.floor(vals.length / 2)] : null;
    const medStr = median != null ? `£${(median * scale).toFixed(2)}` : "—";
    const hist = priceHistogramSvg(vals, scale);
    const flagged = rows.filter(p => (p.honesty_flags || []).length > 0).length;
    const flaggedPct = rows.length ? Math.round(100 * flagged / rows.length) : 0;
    // Top 3 brands by row count (case-insensitive); click filters by that brand.
    const counts = {};
    rows.forEach(p => {
      const b = (p.brand || "").trim();
      if (b) counts[b] = (counts[b] || 0) + 1;
    });
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 3);
    const brandHtml = top.length
      ? top.map(([b, n]) =>
          `<button data-brand="${esc(b)}" class="brand-chip bg-slate-800 hover:bg-sky-800 text-slate-200 px-2 py-0.5 rounded text-[11px]">${esc(b)} <span class="text-slate-500">${n}</span></button>`
        ).join(" ")
      : `<span class="text-slate-500">—</span>`;
    el.innerHTML =
      `<span><b class="text-slate-200">${rows.length}</b> shown / ${inCat.length} in category</span>` +
      `<span>median <b class="text-slate-200">${medStr}</b> ${metricLabel}</span>` +
      `<span>${flagged} flagged <span class="text-slate-500">(${flaggedPct}%)</span></span>` +
      `<span class="flex items-center gap-1">Top brands: ${brandHtml}</span>` +
      (hist ? `<span class="ml-auto">${hist}</span>` : "");
    el.querySelectorAll(".brand-chip").forEach(btn => {
      btn.onclick = () => {
        const f = curFilters();
        f.brand = (f.brand === btn.dataset.brand) ? "" : btn.dataset.brand;
        saveState(); buildFilters(); render();
      };
    });
  }

  function render() {
    renderHead();
    renderModelNote();
    const colDefs = cols();
    const rows = products.filter(passesFilters);
    computeFactorRanges(rows);
    computeDealCutoff(rows);
    rows.sort(cmp);
    renderSummaryBar(rows);
    document.getElementById("empty").classList.toggle("hidden", rows.length > 0);
    document.getElementById("rows").innerHTML = rows.map(p => {
      const fake = (p.honesty_flags || []).includes("impossible_capacity");
      const cls = "border-b border-slate-800 hover:bg-slate-800/40"
        + (fake ? " bg-red-950/40" : "")
        + (p.delisted_at ? " opacity-60" : "");
      return `<tr class="${cls}">${colDefs.map(([, , cellId]) => CELL[cellId](p)).join("")}</tr>`;
    }).join("");
  }

  // ---- controls ----
  function slider(label, value, min, max, step, onInput) {
    const wrap = document.createElement("div");
    wrap.innerHTML = `<div class="flex justify-between text-xs"><span>${label}</span><span class="val text-slate-400">${value}</span></div>`;
    const input = document.createElement("input");
    input.type = "range"; input.min = min; input.max = max; input.step = step;
    input.value = value; input.className = "w-full";
    input.oninput = () => { wrap.querySelector(".val").textContent = input.value; onInput(parseFloat(input.value)); };
    wrap.appendChild(input);
    return wrap;
  }
  function buildFilters() {
    const host = document.getElementById("filter-fields");
    host.innerHTML = "";
    const f = curFilters();
    filterSpecs().forEach(spec => {
      const wrap = document.createElement("label");
      wrap.className = spec.type === "check" ? "flex items-center gap-2" : "block";
      let el;
      if (spec.type === "select") {
        el = document.createElement("select");
        el.innerHTML = spec.options.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
        el.value = f[spec.key] || "";
      } else if (spec.type === "check") {
        el = document.createElement("input"); el.type = "checkbox"; el.checked = !!f[spec.key];
      } else if (spec.type === "range") {
        el = document.createElement("input"); el.type = "range"; el.min = 0; el.max = 100;
        el.value = f[spec.key] || 0;
      } else {
        el = document.createElement("input");
        el.type = spec.type === "num" ? "number" : "text";
        el.value = f[spec.key] || "";
      }
      if (spec.type !== "check") el.className = "w-full bg-slate-800 rounded px-2 py-1 mt-1";
      const valSpan = spec.type === "range" ? `<span class="text-xs text-slate-400 ml-1 fval">${f[spec.key] || 0}</span>` : "";
      if (spec.type === "check") { wrap.append(el, document.createTextNode(" " + spec.label)); }
      else { wrap.innerHTML = spec.label + valSpan; wrap.prepend(); wrap.appendChild(el); }
      el.addEventListener("input", () => {
        f[spec.key] = spec.type === "check" ? el.checked : el.value;
        if (spec.type === "range") wrap.querySelector(".fval").textContent = el.value;
        saveState(); render();
      });
      host.appendChild(wrap);
    });
  }
  function buildFeatureSliders() {
    const fw = document.getElementById("feature-weights");
    fw.innerHTML = "";
    const w = curWeights();
    factors().forEach(([k, label]) =>
      fw.appendChild(slider(label, w[k] || 0, 0, 3, 0.5, v => { w[k] = v; saveState(); render(); })));
  }
  function buildHonestySliders() {
    const hw = document.getElementById("honesty-weights");
    hw.innerHTML = "";
    HONESTY_KEYS.forEach(([k, label]) =>
      hw.appendChild(slider(label, state.honestyWeights[k], 0, 1, 0.05,
        v => { state.honestyWeights[k] = v; saveState(); render(); })));
  }
  function buildCatToggle() {
    const host = document.getElementById("cat-toggle");
    if (!host) return;
    host.innerHTML = "";
    CATEGORIES.forEach(([cat, label]) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.className = "text-sm px-3 py-1 rounded " +
        (state.category === cat ? "bg-sky-600 text-white" : "bg-slate-800 text-slate-300 hover:bg-slate-700");
      b.onclick = () => { if (state.category !== cat) setCategory(cat); };
      host.appendChild(b);
    });
  }
  function setCategory(cat) {
    state.category = cat; saveState();
    buildCatToggle(); buildFilters(); buildFeatureSliders(); render();
  }

  function modelTrained() {
    return products.some(p => p.feature_contrib && Object.keys(p.feature_contrib).length);
  }
  function renderStatus() {
    const s = D.status || {}, lr = s.last_run;
    const parts = [`${products.length} products`];
    if (s.running && s.progress && s.progress.phase !== "idle") {
      const pr = s.progress;
      parts.push(pr.total ? `⏳ ${pr.phase} ${pr.done}/${pr.total}` : (pr.done ? `⏳ ${pr.phase} (${pr.done} found)` : `⏳ ${pr.phase}`));
    } else if (s.running) parts.push("scrape running…");
    if (s.captcha_cooldown_until) {
      const t = new Date(s.captcha_cooldown_until);
      parts.push(`⏸ CAPTCHA cooldown until ${t.toLocaleTimeString()}`);
    }
    if (lr) parts.push(`last run: ${lr.status}${lr.n_found != null ? " (" + lr.n_found + ")" : ""}`);
    if (lr && lr.notes) parts.push(lr.notes);
    const nr = s.next_runs || {};
    if (nr.hourly) parts.push(`refresh: ${new Date(nr.hourly).toLocaleTimeString()}`);
    if (nr.discovery) {
      const d = new Date(nr.discovery);
      const isToday = d.toDateString() === new Date().toDateString();
      parts.push(`discovery: ${isToday ? d.toLocaleTimeString() : d.toLocaleDateString()}`);
    } else if (!nr.hourly && s.next_run) {
      parts.push(`next: ${new Date(s.next_run).toLocaleString()}`);
    }
    document.getElementById("status").textContent = parts.join(" · ");
  }

  // ---- CSV export (current category, filtered + sorted, live weights) ----
  function csvEscape(v) {
    if (v == null) return "";
    const s = String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function exportCsv() {
    const rows = products.filter(passesFilters);
    computeFactorRanges(rows); rows.sort(cmp);
    const cols2 = ["asin", "title", "brand", "category", "chemistry", "url", "price", "avg_price",
      "all_time_low", "all_time_high", "fair_price", "price_delta",
      "claimed_mah", "capacity_wh", "weight_g", "cost_per_mah", "cost_per_wh",
      "pd_w", "usb_c", "usb_a", "ac_output_w", "rating",
      "review_count", "honesty_score", "honesty_flags", "value_0_100"];
    const lines = [cols2.join(",")];
    rows.forEach(p => {
      const hs = honestyScore(p), vy = valueToYou(p);
      lines.push([p.asin, p.title, p.brand, p.category, p.chemistry, p.url, p.price, p.avg_price,
        p.all_time_low, p.all_time_high, p.fair_price, p.price_delta,
        p.claimed_mah, p.capacity_wh, p.weight_g, p.cost_per_mah, p.cost_per_wh,
        p.pd_w, p.usb_c, p.usb_a, p.ac_output_w, p.rating,
        p.review_count, hs != null ? hs.toFixed(1) : "", (p.honesty_flags || []).join("|"),
        vy != null ? Math.round(vy) : ""].map(csvEscape).join(","));
    });
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = state.category + ".csv";
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
  }

  // ---- run buttons + live updates ----
  const runBtn = document.getElementById("runBtn");
  function mkBtn(text, title, onclick) {
    const b = document.createElement("button");
    b.textContent = text; b.title = title;
    b.className = "block w-full mt-2 bg-slate-700 hover:bg-slate-600 text-white text-sm px-3 py-1.5 rounded";
    b.onclick = onclick;
    return b;
  }
  const exportBtn = mkBtn("Export CSV", "Download the current view as CSV", exportCsv);
  runBtn.parentNode.insertBefore(exportBtn, runBtn.nextSibling);
  const fullBtn = mkBtn("Full refresh", "Re-scrape every product's detail page (slower)", () => startRun(true));
  runBtn.parentNode.insertBefore(fullBtn, exportBtn.nextSibling);

  function setRunning(on) { runBtn.disabled = on; runBtn.textContent = on ? "Scrape running…" : "Run scrape now"; }
  function liveTick() {
    // While a scrape runs, poll status every 3s for progress updates.
    // Only reload the full products list when the run finishes — fetching
    // 600+ products with history every 3s is needlessly heavy.
    fetch("/api/status").then(r => r.json()).then(s => {
      D.status = s; renderStatus();
      if (s.running) {
        setRunning(true);
        setTimeout(liveTick, 3000);
      } else {
        setRunning(false);
        const hdrs = _lastProductsModified ? { "If-Modified-Since": _lastProductsModified } : {};
        fetch("/api/products", { headers: hdrs }).then(r => {
          if (r.status === 304) return null;
          _lastProductsModified = r.headers.get("Last-Modified") || _lastProductsModified;
          return r.json();
        }).then(prods => { if (prods) { products = prods; render(); } }).catch(() => {});
      }
    }).catch(() => setTimeout(liveTick, 5000));
  }
  function startRun(full) {
    setRunning(true);
    fetch("/api/run" + (full ? "?full=1" : ""), { method: "POST" })
      .then(r => r.json()).then(() => setTimeout(liveTick, 1500))
      .catch(() => setRunning(false));
  }
  runBtn.onclick = () => startRun(false);

  // ---- watchlist ----
  // A small URL-add form that lives inside the run-button area but only on the
  // Watchlist page. Delete buttons in rows are handled via event delegation so
  // they keep working after every re-render.
  const watchlistBox = document.createElement("section");
  watchlistBox.id = "watchlist-tools";
  watchlistBox.className = "border-t border-slate-800 pt-3 mt-3 space-y-2 hidden";
  watchlistBox.innerHTML = `
    <h2 class="font-semibold text-slate-200">Add Amazon URL</h2>
    <p class="text-xs text-slate-500">Paste any amazon.co.uk product URL — we'll track its price and average.</p>
    <input id="wl-url" type="text" placeholder="https://www.amazon.co.uk/dp/B0..." class="w-full bg-slate-800 rounded px-2 py-1 text-sm" />
    <button id="wl-add" class="block w-full bg-emerald-700 hover:bg-emerald-600 text-white text-sm px-3 py-1.5 rounded">Add to watchlist</button>
    <div id="wl-msg" class="text-xs text-slate-400"></div>`;
  runBtn.parentNode.appendChild(watchlistBox);

  function updateWatchlistVisibility() {
    watchlistBox.classList.toggle("hidden", state.category !== "watchlist");
  }
  updateWatchlistVisibility();
  const _origSetCategory = setCategory;
  setCategory = function (cat) { _origSetCategory(cat); updateWatchlistVisibility(); };
  buildCatToggle();  // rebind so the new setCategory wraps every click

  document.getElementById("wl-add").onclick = () => {
    const input = document.getElementById("wl-url");
    const msg = document.getElementById("wl-msg");
    const url = input.value.trim();
    if (!url) { msg.textContent = "Paste a URL first."; return; }
    msg.textContent = "Adding…";
    fetch("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ url }) })
      .then(r => r.json().then(d => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) { msg.textContent = "Error: " + (d.error || "failed"); return; }
        msg.textContent = `Added ${d.asin} — scraping in the background.`;
        input.value = "";
        setTimeout(liveTick, 1500);  // pick up the placeholder row
      })
      .catch(e => { msg.textContent = "Network error: " + e; });
  };

  document.addEventListener("click", e => {
    const btn = e.target.closest(".watchlist-rm");
    if (!btn) return;
    const asin = btn.dataset.asin;
    const label = btn.dataset.title || asin;
    if (!confirm(`Remove "${label}" from your watchlist?`)) return;
    fetch("/api/watchlist/" + encodeURIComponent(asin), { method: "DELETE" })
      .then(r => r.json()).then(() => liveTick());
  });

  buildHonestySliders();
  buildFilters();
  buildFeatureSliders();
  renderStatus();
  render();
  if ((D.status || {}).running) { setRunning(true); setTimeout(liveTick, 1500); }
})();

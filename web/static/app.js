/* 对账工作台前端：无框架、无构建步骤。所有请求走同源 /api/{ingest,matcher,balance}。 */
(() => {
  "use strict";

  const cfg = window.RECON_CONFIG || { ingest: "/api/ingest", matcher: "/api/matcher", balance: "/api/balance" };
  const state = {
    view: "overview",
    rows: [],
    selectedRow: null,
    candidates: [],
    matches: [],
    healthy: { ingest: null, matcher: null, balance: null },
    strategy: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const yuan = (cents) => {
    if (cents == null || Number.isNaN(cents)) return "—";
    const sign = cents < 0 ? "-" : "";
    const abs = Math.abs(cents);
    return `${sign}${(abs / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };
  const fmtDate = (d) => (d ? String(d).slice(0, 10) : "—");
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      const err = new Error(data?.error?.message || `请求失败（HTTP ${res.status}）`);
      err.code = data?.error?.code;
      err.details = data?.error?.details;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function notice(kind, text, detail) {
    const el = $("#notice");
    el.className = `notice ${kind}`;
    el.hidden = false;
    el.innerHTML = `${esc(text)}${detail ? `<div class="muted" style="margin-top:4px">${esc(detail)}</div>` : ""}`;
    clearTimeout(notice._t);
    notice._t = setTimeout(() => { el.hidden = true; }, kind === "bad" || kind === "warn" ? 9000 : 4200);
  }

  // ---------------------------------------------------------------- health
  async function refreshHealth() {
    const targets = [
      ["ingest", "/health/ingest"],
      ["matcher", "/health/matcher"],
      ["balance", "/health/balance"],
    ];
    const names = { ingest: "接入", matcher: "匹配", balance: "余额" };
    const out = [];
    await Promise.all(targets.map(async ([key, url]) => {
      try {
        const r = await fetch(url, { cache: "no-store" });
        state.healthy[key] = r.ok;
      } catch {
        state.healthy[key] = false;
      }
      out.push(key);
    }));
    $("#health").innerHTML = targets.map(([key]) => {
      const up = state.healthy[key];
      return `<span><i class="dot ${up ? "up" : "down"}"></i>${names[key]}${up === null ? "" : up ? "正常" : "不可用"}</span>`;
    }).join("");
  }

  // ---------------------------------------------------------------- tabs
  function setView(view) {
    state.view = view;
    $$(".tab").forEach((t) => {
      const on = t.dataset.view === view;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
    });
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
    if (view === "overview") loadOverview();
    if (view === "match") loadMatchView();
    if (view === "balance") loadBalance();
    if (view === "drills") loadDrills();
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => setView(t.dataset.view)));

  // ---------------------------------------------------------------- overview
  async function loadOverview() {
    $("#overview-cards").innerHTML = `<div class="skeleton">加载中…</div>`;
    $("#overview-ledger").innerHTML = "";
    try {
      const data = await api(`${cfg.balance}/overview`);
      renderOverview(data);
    } catch (e) {
      $("#overview-cards").innerHTML = "";
      notice("bad", "总览加载失败", e.message);
    }
    refreshHealth();
  }

  function renderOverview(d) {
    const diff = d.raw_diff_cents ?? 0;
    const cards = [
      { label: "银行流水", value: String(d.totals.bank_rows), foot: `合计 ${yuan(d.bank_total_cents)} 元` },
      { label: "账务流水", value: String(d.totals.ledger_rows), foot: `合计 ${yuan(d.ledger_total_cents)} 元` },
      { label: "有效匹配", value: String(d.totals.effective_matches), foot: `接受差额 ${yuan(d.matched_delta_cents)} 元` },
      { label: "账务 − 银行", value: yuan(diff), foot: "元（原始差额）", cls: diff < 0 ? "neg" : diff > 0 ? "pos" : "" },
      { label: "未解释余额", value: yuan(d.identity_check_cents), foot: "应为 0.00；非 0 表示数据不一致", cls: d.identity_check_cents ? "neg" : "" },
    ];
    $("#overview-cards").innerHTML = cards.map((c) => `
      <div class="card ${c.cls || ""}">
        <div class="label">${esc(c.label)}</div>
        <div class="value">${esc(c.value)}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    const lines = (d.narrative || []).map((l) => esc(l));
    const formula = [
      `账务合计        ${yuan(d.ledger_total_cents).padStart(14)}`,
      `银行合计        ${yuan(d.bank_total_cents).padStart(14)}`,
      `原始差额        ${yuan(d.raw_diff_cents).padStart(14)}`,
      `  匹配接受差额  ${yuan(d.matched_delta_cents).padStart(14)}`,
      `  仅账务流水    ${yuan(d.unmatched_ledger.total_cents).padStart(14)}  (${d.unmatched_ledger.count} 笔)`,
      `  仅银行流水    ${yuan(-d.unmatched_bank.total_cents).padStart(14)}  (${d.unmatched_bank.count} 笔)`,
      `校验余额        ${yuan(d.identity_check_cents).padStart(14)}`,
    ].join("\n");
    $("#overview-ledger").innerHTML = `
      <div class="formula">${formula}</div>
      <ul style="margin:0;padding-left:18px;color:var(--muted)">${lines.map((l) => `<li>${l}</li>`).join("")}</ul>
      ${d.matcher_down ? `<div class="notice warn" style="margin:0">匹配服务不可用：当前只展示流水合计，未达账项与匹配归因不可用。</div>` : ""}`;
  }

  // ---------------------------------------------------------------- match view
  async function loadMatchView() {
    await Promise.all([loadRows(), loadMatches()]);
    if (state.selectedRow) selectRow(state.selectedRow.id, { keepScroll: true });
  }

  function rowQuery() {
    const params = new URLSearchParams();
    const src = $("#rows-source").value;
    const status = $("#rows-status").value;
    const late = $("#rows-late").value;
    const q = $("#rows-search").value.trim();
    if (src) params.set("source", src);
    if (status) params.set("status", status);
    if (late) params.set("late", late);
    if (q) params.set("q", q);
    params.set("limit", "300");
    return params.toString();
  }

  async function loadRows() {
    try {
      const data = await api(`${cfg.ingest}/rows?${rowQuery()}`);
      state.rows = data.rows;
      $("#rows-count").textContent = `${data.total} 笔`;
      renderRows();
    } catch (e) {
      $("#row-list").innerHTML = `<div class="empty">流水加载失败：${esc(e.message)}</div>`;
    }
  }

  function renderRows() {
    if (!state.rows.length) {
      $("#row-list").innerHTML = `<div class="empty">没有符合条件的流水。</div>`;
      return;
    }
    $("#row-list").innerHTML = state.rows.map((r) => {
      const amtCls = r.direction === "credit" ? "credit" : "debit";
      const selected = state.selectedRow && state.selectedRow.id === r.id ? "selected" : "";
      return `<button class="row-item ${selected}" data-row="${esc(r.id)}" type="button">
        <span class="date">${esc(fmtDate(r.value_date))}</span>
        <span>
          <div class="ref">${esc(r.external_ref)} <span class="chip ${r.source}">${r.source === "bank" ? "银行" : "账务"}</span>
          ${r.late ? `<span class="chip late">迟到</span>` : ""}</div>
          <div class="memo">${esc(r.memo || r.counterparty || "—")}</div>
        </span>
        <span class="amt ${amtCls}">${r.direction === "credit" ? "+" : "−"}${yuan(r.amount_cents)}</span>
      </button>`;
    }).join("");
    $$("#row-list .row-item").forEach((el) => el.addEventListener("click", () => selectRow(el.dataset.row)));
  }

  async function selectRow(rowId) {
    const row = state.rows.find((r) => r.id === rowId) || null;
    state.selectedRow = row;
    renderRows();
    const panel = $("#row-detail");
    panel.innerHTML = `<div class="skeleton">加载候选…</div>`;
    try {
      const [cand, rowMatches] = await Promise.all([
        api(`${cfg.matcher}/candidates?row_id=${encodeURIComponent(rowId)}&limit=40`),
        api(`${cfg.matcher}/matches?row_id=${encodeURIComponent(rowId)}`),
      ]);
      state.candidates = cand.candidates;
      renderDetail(row, cand, rowMatches.matches);
    } catch (e) {
      panel.innerHTML = `<div class="empty">候选加载失败：${esc(e.message)}</div>`;
    }
  }

  function renderDetail(row, cand, rowMatches) {
    if (!row) { $("#row-detail").innerHTML = `<div class="detail-empty">选择一笔流水查看候选依据</div>`; return; }
    const amtCls = row.direction === "credit" ? "credit" : "debit";
    const effective = rowMatches.filter((m) => m.effective);
    const rejected = rowMatches.filter((m) => !m.effective && m.status === "rejected");
    const head = `
      <div class="detail-head">
        <div>
          <h2>${esc(row.external_ref)} ${row.late ? `<span class="chip late">迟到</span>` : ""}</h2>
          <div class="detail-meta">${esc(fmtDate(row.value_date))} · ${esc(row.account)} · ${esc(row.counterparty || "")}</div>
          <div class="detail-meta">${esc(row.memo || "")}</div>
        </div>
        <div class="amt ${amtCls}" style="font-size:19px;font-weight:650">${row.direction === "credit" ? "+" : "−"}${yuan(row.amount_cents)}</div>
      </div>
      <div class="muted" style="margin-bottom:10px">${row.source === "bank" ? "银行侧流水：候选来自账务侧。" : "账务侧流水：候选来自银行侧。"}</div>`;

    let occupied = "";
    if (effective.length) {
      occupied = `<div class="notice ok" style="margin-bottom:12px">该流水已在 ${effective.length} 个有效匹配中：${effective.map((m) => `<b>${esc(m.kind)}</b> · ${esc(m.status === "confirmed" ? "人工确认" : "自动提案")}`).join("；")}</div>`;
    }
    if (rejected.length) {
      occupied += `<div class="notice warn" style="margin-bottom:12px">曾被人工否决 ${rejected.length} 次：${rejected.map((m) => esc(m.note || "无备注")).join("；")}</div>`;
    }

    const cands = cand.candidates;
    const list = cands.length ? cands.map((c, i) => {
      const otherSide = row.source === "bank" ? c.ledger_ids : c.bank_ids;
      const pct = clamp(c.score, 0, 100);
      const parts = Object.entries(c.parts || {}).filter(([, v]) => v > 0)
        .map(([k, v]) => `${partName(k)} ${v.toFixed(1)}`).join(" · ");
      return `<div class="cand ${i === 0 ? "best" : ""}" data-cand="${esc(c.key)}">
        <div class="cand-head">
          <div><b>${kindName(c.kind)}</b> <span class="muted">${otherSide.length} 笔对手方</span></div>
          <div class="cand-score">${c.score.toFixed(1)} 分</div>
        </div>
        <div class="bar"><i style="width:${pct}%"></i></div>
        <ul>${(c.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <div class="muted" style="font-size:11px;margin-top:5px">分项：${esc(parts || "—")} · 差额 ${yuan(c.amount_delta_cents)} 元</div>
        <div class="cand-actions">
          <button class="btn small" data-act="confirm" data-key="${esc(c.key)}">确认匹配</button>
          <button class="btn small ghost" data-act="reject" data-key="${esc(c.key)}">否决</button>
          <button class="btn small ghost" data-act="explain" data-key="${esc(c.key)}">查看明细</button>
        </div>
      </div>`;
    }).join("") : `<div class="empty">当前策略下没有可用候选。可查看“未达账项”或调整策略窗口后重试。</div>`;

    $("#row-detail").innerHTML = head + occupied + `<h3>候选依据（${cands.length}）</h3>` + list;
    $$("#row-detail .cand-actions button").forEach((b) => b.addEventListener("click", () => handleCand(b.dataset.act, b.dataset.key)));
  }

  const partName = (k) => ({ amount: "金额", date: "日期", reference: "引用", account: "账号", counterparty: "对手" }[k] || k);
  const kindName = (k) => ({ one_to_one: "一对一", split: "拆单 1→N", merge: "合单 N→1" }[k] || k);

  function candidateByKey(key) { return state.candidates.find((c) => c.key === key); }

  async function handleCand(act, key) {
    const c = candidateByKey(key);
    if (!c) return;
    const row = state.selectedRow;
    if (act === "explain") {
      const pct = Object.entries(c.parts || {}).map(([k, v]) => `<span class="chip">${esc(partName(k))} ${v.toFixed(1)}</span>`).join("");
      openDialog("候选构成", `
        <p><b>${esc(kindName(c.kind))}</b> · ${c.score.toFixed(1)} 分 · 差额 ${yuan(c.amount_delta_cents)} 元</p>
        <ul style="margin:8px 0 0 18px;color:var(--muted)">${(c.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <div class="chips" style="margin-top:10px">${pct}</div>
        <p class="muted" style="margin-top:8px">银行 ${c.bank_ids.length} 笔 / 账务 ${c.ledger_ids.length} 笔。确认前会校验唯一占用与策略边界。</p>`, [{ label: "关闭", action: "close" }]);
      return;
    }
    const isConfirm = act === "confirm";
    const body = { bank_ids: c.bank_ids, ledger_ids: c.ledger_ids };
    openDialog(isConfirm ? "确认为人工匹配" : "否决候选", `
      <p>${isConfirm ? "确认后该组合成为有效匹配，不再被自动重算改动。" : "否决后该组合在后续自动匹配中会被抑制。"}</p>
      <div class="muted" style="margin-top:6px">${esc(kindName(c.kind))} · ${c.score.toFixed(1)} 分 · 差额 ${yuan(c.amount_delta_cents)} 元</div>
      <div id="dlg-extra" class="chips" style="margin-top:10px"></div>`,
      [
        { label: "取消", action: "close", cls: "ghost" },
        { label: isConfirm ? "确认" : "否决", action: isConfirm ? "confirm" : "reject", cls: isConfirm ? "" : "danger" },
      ],
      async (action) => {
        if (action === "close") return true;
        const note = $("#dlg-note").value.trim();
        if (!isConfirm && !note) { notice("warn", "否决必须填写原因"); return false; }
        try {
          if (isConfirm) {
            await api(`${cfg.matcher}/candidates/decide`, { method: "POST", body: { ...body, action: "confirm", note } });
            notice("ok", "已确认匹配");
          } else {
            await api(`${cfg.matcher}/candidates/decide`, { method: "POST", body: { ...body, action: "reject", note } });
            notice("ok", "已否决；后续自动匹配不会重新提出该组合");
          }
          await loadMatchView();
          return true;
        } catch (e) {
          if (e.code === "warnings_unacknowledged") {
            const extra = $("#dlg-extra");
            extra.innerHTML = `<div class="notice warn" style="margin:0;width:100%">${esc(e.message)}：
              <ul style="margin:6px 0 0 16px">${(e.details || []).map((d) => `<li>${esc(d)}</li>`).join("")}</ul></div>
              <label class="field check"><input type="checkbox" id="dlg-ack" /> 我已知悉并承担人工覆盖的责任</label>`;
            $("#dlg-ok").onclick = async () => {
              if (!$("#dlg-ack")?.checked) { notice("warn", "请先勾选确认"); return; }
              try {
                await api(`${cfg.matcher}/candidates/decide`, { method: "POST", body: { ...body, action: "confirm", note, acknowledge_warnings: true } });
                closeDialog();
                notice("ok", "已确认匹配（含人工覆盖记录）");
                await loadMatchView();
              } catch (err) {
                notice("bad", "确认失败", err.message);
              }
            };
            return false;
          }
          notice("bad", "操作失败", `${e.message}${e.details ? " · " + JSON.stringify(e.details) : ""}`);
          return false;
        }
      });
  }

  // ---------------------------------------------------------------- matches
  async function loadMatches() {
    try {
      const data = await api(`${cfg.matcher}/matches?status=proposed,confirmed,stale,rejected`);
      state.matches = data.matches;
      $("#matches-count").textContent = `${data.matches.length} 条`;
      renderMatches();
    } catch (e) {
      $("#match-list").innerHTML = `<div class="empty">匹配加载失败：${esc(e.message)}</div>`;
    }
  }

  const statusName = (s) => ({ proposed: "自动提案", confirmed: "人工确认", rejected: "人工否决", stale: "确认已失效", released: "人工已释放" }[s] || s);

  function renderMatches() {
    const rows = state.matches;
    if (!rows.length) { $("#match-list").innerHTML = `<div class="empty">尚无匹配记录，先运行一次自动匹配。</div>`; return; }
    $("#match-list").innerHTML = rows.map((m) => {
      const acts = [];
      if (m.status === "proposed") {
        acts.push(`<button class="btn small" data-mact="confirm" data-id="${esc(m.id)}">确认</button>`);
        acts.push(`<button class="btn small ghost" data-mact="reject" data-id="${esc(m.id)}">否决</button>`);
      }
      if (m.status === "confirmed" || m.status === "stale") {
        acts.push(`<button class="btn small ghost" data-mact="release" data-id="${esc(m.id)}">释放</button>`);
      }
      if (m.status === "stale" && m.origin === "manual") {
        acts.push(`<button class="btn small" data-mact="reconfirm" data-id="${esc(m.id)}">重新确认</button>`);
      }
      if (m.status === "rejected" || m.status === "stale" || m.status === "released") {
        acts.push(`<button class="btn small ghost" data-mact="reinstate" data-id="${esc(m.id)}">重新激活</button>`);
      }
      return `<div class="match-row">
        <div class="top">
          <span class="chip ${m.status}">${esc(statusName(m.status))}</span>
          <span class="chip">${esc(kindName(m.kind))}</span>
          <span class="chip">${m.origin === "manual" ? "人工" : "自动"}</span>
          <span class="muted" style="margin-left:auto">${m.score.toFixed(1)} 分 · 差额 ${yuan(m.amount_delta_cents)} 元</span>
        </div>
        <div class="arrows">银行 ${m.bank_ids.length} 笔 ↔ 账务 ${m.ledger_ids.length} 笔${m.effective ? "" : " · 不参与对账"}</div>
        ${(m.reasons || []).slice(0, 2).map((r) => `<div class="muted" style="font-size:12px">· ${esc(r)}</div>`).join("")}
        ${m.note ? `<div class="muted" style="font-size:12px">备注：${esc(m.note)}</div>` : ""}
        <div class="match-actions">${acts.join("")}</div>
      </div>`;
    }).join("");
    $$("#match-list button[data-mact]").forEach((b) => b.addEventListener("click", () => handleMatchAction(b.dataset.mact, b.dataset.id)));
  }

  async function handleMatchAction(act, id) {
    const m = state.matches.find((x) => x.id === id);
    if (!m) return;
    if (act === "release") {
      openDialog("释放人工匹配", `
        <p>释放后该确认不再参与对账。请选择重配范围——全局重配成本更高，但会为所有流水重新寻找最优组合。</p>
        <div class="chips" id="release-mode" style="margin-top:10px">
          <label class="chip"><input type="radio" name="relmode" value="none" checked /> 仅释放</label>
          <label class="chip"><input type="radio" name="relmode" value="local" /> 局部重配</label>
          <label class="chip"><input type="radio" name="relmode" value="global" /> 全局重配</label>
        </div>`,
        [{ label: "取消", action: "close", cls: "ghost" }, { label: "释放", action: "release", cls: "danger" }],
        async (action) => {
          if (action === "close") return true;
          const mode = $('input[name="relmode"]:checked')?.value || "none";
          try {
            const res = await api(`${cfg.matcher}/matches/${id}/release`, { method: "POST", body: { mode: "none", note: $("#dlg-note").value.trim() } });
            closeDialog();
            let msg = "已释放该人工确认";
            if (mode === "global") {
              const r = await api(`${cfg.matcher}/run`, { method: "POST", body: { mode: "global", scope: "release-global" } });
              msg += `；全局重配 ${r.summary.selected} 组，耗时 ${r.summary.elapsed_ms}ms`;
              logDrill(`全局重配 ${r.summary.selected} 组 / ${r.summary.elapsed_ms}ms`);
            } else if (mode === "local") {
              const r = await api(`${cfg.matcher}/run`, { method: "POST", body: { mode: "local", release_match_ids: [id] } });
              msg += `；局部重配 ${r.summary.selected} 组（仅 ${r.summary.candidates_generated} 个候选），耗时 ${r.summary.elapsed_ms}ms`;
              logDrill(`局部重配 ${r.summary.selected} 组 / ${r.summary.candidates_generated} 候选 / ${r.summary.elapsed_ms}ms`);
            }
            notice("ok", msg);
            await loadMatchView();
            return true;
          } catch (e) {
            notice("bad", "释放失败", e.message);
            return false;
          }
        });
      return;
    }
    if (act === "reconfirm") {
      openDialog("重新确认已失效匹配", `
        <p>该人工确认在策略变更后不再满足新规则，因此暂时不参与对账。重新确认后它会以新策略重新生效；证据不足的部分会作为人工覆盖记录在案。</p>
        <div class="muted" style="margin-top:6px">${esc(kindName(m.kind))} · 银行 ${m.bank_ids.length} 笔 ↔ 账务 ${m.ledger_ids.length} 笔</div>`,
        [{ label: "取消", action: "close", cls: "ghost" }, { label: "重新确认", action: "go" }],
        async (action) => {
          if (action === "close") return true;
          try {
            await api(`${cfg.matcher}/matches/${id}/confirm`, {
              method: "POST", body: { note: $("#dlg-note").value.trim(), acknowledge_warnings: true },
            });
            closeDialog();
            notice("ok", "已重新确认，匹配恢复有效");
            await loadMatchView();
            return true;
          } catch (e) {
            notice("bad", "重新确认失败", e.message);
            return false;
          }
        });
      return;
    }
    if (act === "reinstate") {
      try {
        await api(`${cfg.matcher}/matches/${id}/reinstate`, { method: "POST", body: {} });
        notice("ok", "已重新激活为候选，需再次确认");
        await loadMatchView();
      } catch (e) { notice("bad", "操作失败", e.message); }
      return;
    }
    const isConfirm = act === "confirm";
    openDialog(isConfirm ? "确认匹配" : "否决匹配", `
      <p>${isConfirm ? "确认后成为有效匹配。" : "否决后不再参与对账，且相同组合不会被自动重提。"}</p>`,
      [{ label: "取消", action: "close", cls: "ghost" }, { label: isConfirm ? "确认" : "否决", action: "go", cls: isConfirm ? "" : "danger" }],
      async (action) => {
        if (action === "close") return true;
        const note = $("#dlg-note").value.trim();
        if (!isConfirm && !note) { notice("warn", "否决必须填写原因"); return false; }
        try {
          await api(`${cfg.matcher}/matches/${id}/${isConfirm ? "confirm" : "reject"}`, {
            method: "POST", body: { note, acknowledge_warnings: isConfirm || undefined },
          });
          closeDialog();
          notice("ok", isConfirm ? "已确认" : "已否决");
          await loadMatchView();
          return true;
        } catch (e) {
          notice("bad", "操作失败", e.message);
          return false;
        }
      });
  }

  // ---------------------------------------------------------------- late rows
  async function loadBalance() {
    $("#balance-cards").innerHTML = `<div class="skeleton">加载中…</div>`;
    try {
      const d = await api(`${cfg.balance}/overview`);
      const diff = d.raw_diff_cents ?? 0;
      $("#balance-degraded").innerHTML = d.matcher_down
        ? `<div class="notice warn">匹配服务不可用：下面只按流水合计计算，“仅见”两侧包含全部流水，
           并不代表真的未达；快照与漂移不可用。恢复匹配服务后刷新即可。</div>`
        : "";
      $("#balance-cards").innerHTML = [
        ["原始差额", yuan(diff), "账务合计 − 银行合计"],
        ["匹配接受差额", d.matcher_down ? "—" : yuan(d.matched_delta_cents), d.matcher_down ? "匹配不可用" : "已匹配组内部的差额"],
        ["仅见银行（冲减差额）", yuan(d.unmatched_bank.contribution_cents), `${d.unmatched_bank.count} 笔，银行合计 ${yuan(d.unmatched_bank.total_cents)}`],
        ["仅见账务（增加差额）", yuan(d.unmatched_ledger.contribution_cents), `${d.unmatched_ledger.count} 笔，账务合计 ${yuan(d.unmatched_ledger.total_cents)}`],
        ["校验余额", yuan(d.identity_check_cents), "应为 0.00；非 0 表示数据不一致"],
      ].map(([l, v, foot]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div><div class="foot">${esc(foot)}</div></div>`).join("");
      const mini = (rows, side) => rows.length ? rows.map((r) => `
        <div class="mini">
          <span><b>${esc(r.external_ref)}</b><br><span class="muted">${esc(fmtDate(r.value_date))} · ${esc(bucketName(r.bucket))}</span>
            ${r.hint ? `<br><span class="muted">最接近：${esc(r.hint.external_ref || r.hint.row_id.slice(-6))}，差 ${yuan(r.hint.delta_cents)} 元 / ${r.hint.days} 天</span>` : ""}</span>
          <span class="amt ${r.amount_yuan?.startsWith("-") ? "debit" : "credit"}">${esc(r.amount_yuan)}</span>
        </div>`).join("") : `<div class="empty">无</div>`;
      $("#unmatched-bank").innerHTML = mini(d.unmatched_bank.rows);
      $("#unmatched-ledger").innerHTML = mini(d.unmatched_ledger.rows);
      const periods = d.periods || [];
      const periodCheck = d.period_check_cents ?? 0;
      $("#period-table").innerHTML = periods.length ? `<table>
        <thead><tr><th>期间</th><th>迟到（贡献）</th><th>金额不符（贡献）</th><th>其他未达（贡献）</th><th>匹配差额</th><th>小计</th></tr></thead>
        <tbody>${periods.map((p) => `<tr>
            <td>${esc(p.month)}</td><td>${yuan(p.late_cents)}</td><td>${yuan(p.amount_cents)}</td>
            <td>${yuan(p.standard_cents)}</td><td>${yuan(p.match_delta_cents)}</td><td>${yuan(p.contribution_cents)}</td></tr>`).join("")}
          <tr><td><b>合计</b></td><td colspan="4" class="muted">各期间贡献之和应等于原始差额</td>
            <td><b>${yuan(d.period_contribution_total_cents)}</b></td></tr></tbody></table>
        <p class="muted" style="margin-top:8px">期间合计 − 原始差额 = ${yuan(periodCheck)}（应为 0.00；银行侧未达按冲减计，账务侧未达按增加计）。</p>` : `<div class="empty">暂无数据</div>`;
    } catch (e) {
      notice("bad", "余额分析失败", e.message);
      $("#balance-cards").innerHTML = "";
      $("#balance-degraded").innerHTML = "";
    }
    refreshHealth();
  }
  const bucketName = (b) => ({ late: "迟到", amount: "金额不符", standard: "未达账项" }[b] || b || "—");

  // ---------------------------------------------------------------- drills
  async function loadDrills() {
    $("#drill-cards").innerHTML = `
      <div class="card"><div class="label">演示数据</div><div class="value">虚构</div>
        <div class="foot">所有主体与金额均为虚构，不发起真实转账</div></div>
      <div class="card"><div class="label">补入迟到流水</div>
        <div class="foot" style="margin-top:8px"><button class="btn small" id="add-late">补入银行+账务迟到流水</button></div></div>
      <div class="card"><div class="label">运行匹配</div>
        <div class="foot" style="margin-top:8px">
          <button class="btn small" id="run-global">全局重配</button>
          <button class="btn small ghost" id="run-preview">仅预览</button>
        </div></div>`;
    $("#add-late").onclick = addLateRows;
    $("#run-global").onclick = () => runPlan("global");
    $("#run-preview").onclick = async () => {
      try {
        const r = await api(`${cfg.matcher}/run`, { method: "POST", body: { mode: "preview" } });
        logDrill(`预览：锁定人工 ${r.locked_manual.length} 组；将失效 ${r.would_stale.length} 组`);
      } catch (e) { logDrill(`预览失败：${e.message}`); }
    };
    loadStrategy();
    loadSimEvents();
  }

  async function loadStrategy() {
    try {
      const data = await api(`${cfg.matcher}/strategy`);
      state.strategy = data.strategy;
      const s = data.strategy;
      $("#strategy-form").innerHTML = `
        <label class="field"><span>策略名称</span><input id="st-name" value="${esc(s.name)}" /></label>
        <label class="field"><span>日期窗口（天）</span><input id="st-window" type="number" min="0" max="30" value="${s.day_window}" /></label>
        <label class="field"><span>金额容差（分）</span><input id="st-tol" type="number" min="0" max="100000" value="${s.amount_tolerance_cents}" /></label>
        <label class="field"><span>最低分数</span><input id="st-score" type="number" min="0" max="100" step="0.5" value="${s.min_score}" /></label>
        <label class="field"><span>单组最多拆/合笔数</span><input id="st-parts" type="number" min="2" max="10" value="${s.max_parts}" /></label>
        <label class="field check"><input type="checkbox" id="st-split" ${s.allow_split ? "checked" : ""} /><span>允许拆单</span></label>
        <label class="field check"><input type="checkbox" id="st-merge" ${s.allow_merge ? "checked" : ""} /><span>允许合单</span></label>
        <div class="field"><span>当前策略指纹</span><span class="chip">${esc(data.hash)}</span></div>`;
    } catch (e) {
      $("#strategy-form").innerHTML = `<div class="empty">策略加载失败：${esc(e.message)}</div>`;
    }
  }

  $("#save-strategy").addEventListener("click", async () => {
    const body = {
      name: $("#st-name").value,
      day_window: Number($("#st-window").value),
      amount_tolerance_cents: Number($("#st-tol").value),
      min_score: Number($("#st-score").value),
      max_parts: Number($("#st-parts").value),
      allow_split: $("#st-split").checked,
      allow_merge: $("#st-merge").checked,
    };
    try {
      const r = await api(`${cfg.matcher}/strategy`, { method: "PUT", body });
      notice("ok", "策略已保存", "需要重新运行匹配才会生效；人工确认不会被静默改写。");
      logDrill(`策略更新 ${r.hash}，待重算提案 ${r.pending_proposals_require_rerun} 条`);
      await loadStrategy();
    } catch (e) {
      notice("bad", "策略保存失败", `${e.message}${e.details ? " · " + JSON.stringify(e.details) : ""}`);
    }
  });

  async function runPlan(mode, releaseIds = []) {
    try {
      const r = await api(`${cfg.matcher}/run`, { method: "POST", body: { mode, release_match_ids: releaseIds } });
      const s = r.summary || {};
      notice("ok", `匹配完成（${mode}）`, `选中 ${s.selected} 组 · 人工锁定 ${s.locked_manual} · 失效 ${s.stale_manual} · 否决抑制 ${s.suppressed_rejected} · 耗时 ${s.elapsed_ms}ms`);
      logDrill(`run ${mode}: +${s.selected} / -${s.removed_auto} auto, lock ${s.locked_manual}, stale ${s.stale_manual}, ${s.elapsed_ms}ms`);
      if (mode === "global" || mode === "local") await loadMatchView();
      return r;
    } catch (e) {
      notice("bad", "匹配失败", e.message);
      logDrill(`run ${mode} 失败：${e.message}`);
    }
  }

  async function addLateRows() {
    try {
      const late = await fetch("/static/late.json").then((r) => r.json());
      let added = 0;
      for (const source of ["bank", "ledger"]) {
        const res = await api(`${cfg.ingest}/batches`, {
          method: "POST",
          body: { source, label: "迟到补录", note: "演示：业务日期早于当前水位", rows: late[source] },
        });
        added += res.inserted_count;
        if (res.duplicates.length) logDrill(`${source}: ${res.duplicates.length} 笔重复被拒绝`);
      }
      notice("ok", `已补入 ${added} 笔迟到流水`, "刷新后可见“迟到”标记；重新匹配可将其纳入。");
      logDrill(`补入迟到流水 ${added} 笔`);
      await Promise.all([loadRows(), loadMatches()]);
    } catch (e) {
      notice("bad", "补入失败", e.message);
      logDrill(`补入失败：${e.message}`);
    }
  }

  function logDrill(text) {
    const el = $("#drill-log");
    if (!el) return;
    const line = document.createElement("div");
    line.textContent = `${new Date().toLocaleTimeString("zh-CN")}  ${text}`;
    el.prepend(line);
  }

  async function loadSimEvents() {
    try {
      const d = await api(`${cfg.matcher}/simulate/events`);
      const el = $("#drill-log");
      if (el && !el.childElementCount) {
        el.innerHTML = d.events.length
          ? d.events.map((e) => `<div>${esc(e.started_at)}  ${esc(e.kind)} · ${esc(e.outcome || "")} ${esc(e.detail || "")}</div>`).join("")
          : `<div class="muted">暂无演练记录</div>`;
      }
    } catch { /* matcher down is shown in health */ }
  }

  $("#drill-interrupt").addEventListener("click", async () => {
    try {
      const before = await api(`${cfg.matcher}/effective`);
      await api(`${cfg.matcher}/simulate/interrupt`, { method: "POST", body: { fail_after: 0 } }).catch((e) => { throw e; });
      logDrill("中断演练：未触发异常（无匹配可写）");
    } catch (e) {
      if (e.code === "simulated_interrupt") {
        const after = await api(`${cfg.matcher}/effective`);
        logDrill("中断演练：写入中途失败，事务回滚；旧匹配保持原样");
        notice("warn", "模拟匹配中断", "事务已回滚，没有留下半成品关系。可继续操作验证一致性。");
      } else {
        logDrill(`中断演练失败：${e.message}`);
        notice("bad", "中断演练失败", e.message);
      }
    }
    loadSimEvents();
    refreshHealth();
  });

  $("#drill-outage").addEventListener("click", async () => {
    try {
      const r = await api(`${cfg.matcher}/simulate/outage`, { method: "POST", body: { seconds: 5 } });
      logDrill(`服务中断演练：${r.outage_seconds} 秒内匹配接口返回 503`);
      notice("warn", "匹配服务已进入演练中断", "余额页会显示“匹配不可用”，不会伪造已对账结果。");
      setTimeout(refreshHealth, 700);
      setTimeout(() => { refreshHealth(); logDrill("中断结束，匹配服务恢复"); }, 5300);
    } catch (e) {
      notice("bad", "中断演练失败", e.message);
    }
  });

  $("#drill-recover").addEventListener("click", async () => {
    await refreshHealth();
    if (!state.healthy.matcher) {
      notice("warn", "匹配服务仍未恢复", "请稍候重试；余额分析在匹配不可用时只展示流水合计。");
      return;
    }
    await runPlan("global");
    await loadSimEvents();
    refreshHealth();
  });

  // ---------------------------------------------------------------- dialog
  let dialogHandler = null;
  function openDialog(title, html, buttons, handler) {
    $("#dlg-title").textContent = title;
    $("#dlg-content").innerHTML = html;
    $("#dlg-note").value = "";
    const primary = buttons.find((b) => b.action !== "close") || buttons[0];
    $("#dlg-ok").textContent = primary?.label || "确定";
    $("#dlg-ok").className = `btn ${primary?.cls || ""}`;
    $("#confirm-dialog").hidden = false;
    $("#dlg-note").focus();
    dialogHandler = handler;
    $("#dlg-ok").onclick = async () => {
      const action = primary?.action;
      const ok = dialogHandler ? await dialogHandler(action) : true;
      if (ok) closeDialog();
    };
    $("#dlg-cancel").onclick = () => closeDialog();
  }
  function closeDialog() {
    $("#confirm-dialog").hidden = true;
    dialogHandler = null;
  }
  $("#confirm-dialog").addEventListener("click", (e) => { if (e.target.id === "confirm-dialog") closeDialog(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#confirm-dialog").hidden) closeDialog(); });

  // ---------------------------------------------------------------- misc
  $("#refresh-overview").onclick = loadOverview;
  $("#refresh-balance").onclick = loadBalance;
  ["#rows-source", "#rows-status", "#rows-late"].forEach((s) => $(s).addEventListener("change", loadRows));
  let searchTimer = null;
  $("#rows-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadRows, 220);
  });

  refreshHealth();
  setInterval(refreshHealth, 15000);
  setView("overview");

  // Exposed for the headless UI test only; the page itself never calls this.
  window.__recon = {
    state,
    setView,
    selectRow,
    loadRows,
    loadMatches,
    refreshHealth,
    handleCand,
    handleMatchAction,
    runPlan,
    addLateRows,
  };
})();

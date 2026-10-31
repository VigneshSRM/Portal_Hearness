/* Redmine Agent dashboard - vanilla JS, no build step. */
(function () {
  "use strict";
  const PALETTE = ["#4f46e5", "#12b76a", "#f79009", "#d92d20", "#0ba5ec", "#7a5af8", "#ee46bc", "#667085", "#16b364", "#fb6514", "#2e90fa", "#a15c07"];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  if (window.Chart) {
    Chart.defaults.font.family = getComputedStyle(document.documentElement).fontFamily;
    Chart.defaults.color = "#475467";
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    Chart.defaults.maintainAspectRatio = false;
  }

  const H = {
    templates: [],
    authUser: "",
    reportColumns: {},
    reportPresets: {},

    // ------------------------------------------------------------ basics
    toast(msg, isError) {
      const t = $("#toast");
      t.textContent = msg;
      t.className = "toast show" + (isError ? " error" : "");
      clearTimeout(H._toastTimer);
      H._toastTimer = setTimeout(() => (t.className = "toast"), isError ? 7000 : 3500);
    },
    getReviewer() {
      return H.authUser || localStorage.getItem("harness.reviewer") || "";
    },
    setReviewer(v) {
      localStorage.setItem("harness.reviewer", (v || "").trim());
    },
    initReviewer() {
      const input = $("#reviewerName");
      if (input) input.value = localStorage.getItem("harness.reviewer") || "";
    },
    requireReviewer() {
      const r = H.getReviewer();
      if (!r) {
        H.toast("Enter your name as reviewer first (top right).", true);
        const input = $("#reviewerName");
        if (input) input.focus();
        throw new Error("no reviewer");
      }
      return r;
    },
    async api(method, url, body) {
      const headers = { "X-Harness-Request": "1", "Accept": "application/json" };
      const reviewer = H.getReviewer();
      if (reviewer) headers["X-Harness-Reviewer"] = reviewer;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const resp = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      let data = null;
      try { data = await resp.json(); } catch (e) { data = null; }
      if (!resp.ok) {
        const msg = (data && (data.error || data.detail)) || `HTTP ${resp.status}`;
        const details = data && data.details ? " - " + data.details.join("; ") : "";
        throw new Error(msg + details);
      }
      return data;
    },
    async refresh() {
      try { await H.api("POST", "/api/cache/clear"); } catch (e) { /* ignore */ }
      location.reload();
    },
    filterTable(input, tableId) {
      const q = input.value.toLowerCase();
      $$(`#${tableId} tbody tr`).forEach((tr) => {
        tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    },

    // ------------------------------------------------------------ charts
    _chart(id, config) {
      const el = document.getElementById(id);
      if (!el || !window.Chart) return null;
      return new Chart(el, config);
    },
    _ds(datasets, type) {
      return datasets.map((d, i) => ({
        label: d.label, data: d.data,
        backgroundColor: d.color || PALETTE[i % PALETTE.length] + (type === "line" ? "22" : ""),
        borderColor: d.color || PALETTE[i % PALETTE.length],
        borderWidth: type === "line" ? 2 : 0, borderRadius: type === "line" ? 0 : 4,
        tension: 0.3, fill: type === "line", pointRadius: type === "line" ? 0 : undefined, pointHitRadius: 8,
      }));
    },
    line(id, labels, datasets) {
      return H._chart(id, { type: "line", data: { labels, datasets: H._ds(datasets, "line") },
        options: { interaction: { mode: "index", intersect: false }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } }, x: { ticks: { maxTicksLimit: 12 } } } } });
    },
    bar(id, labels, datasets, opts = {}) {
      const ds = H._ds(datasets, "bar");
      if (opts.colorByValue) {
        ds[0].backgroundColor = datasets[0].data.map((v) => (v == null ? "#d0d5dd" : v >= 90 ? "#12b76a" : v >= 75 ? "#f79009" : "#d92d20"));
      }
      return H._chart(id, { type: "bar", data: { labels, datasets: ds },
        options: { plugins: { legend: { display: datasets.length > 1 } },
          scales: { y: { beginAtZero: true, max: opts.max, ticks: { callback: (v) => (opts.percent ? v + "%" : v) } }, x: { ticks: { maxTicksLimit: 16 } } } } });
    },
    stacked(id, labels, datasets) {
      return H._chart(id, { type: "bar", data: { labels, datasets: H._ds(datasets, "bar") },
        options: { scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } } } });
    },
    hbar(id, rows) {
      rows = rows || [];
      return H._chart(id, { type: "bar",
        data: { labels: rows.map((r) => r.label), datasets: [{ label: "", data: rows.map((r) => r.value), backgroundColor: PALETTE[0], borderRadius: 4 }] },
        options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true } } } });
    },
    hbarMulti(id, labels, datasets) {
      return H._chart(id, { type: "bar", data: { labels, datasets: H._ds(datasets, "bar") },
        options: { indexAxis: "y", scales: { x: { beginAtZero: true } } } });
    },
    doughnut(id, rows) {
      rows = rows || [];
      return H._chart(id, { type: "doughnut",
        data: { labels: rows.map((r) => r.label), datasets: [{ data: rows.map((r) => r.value), backgroundColor: PALETTE, borderWidth: 1 }] },
        options: { cutout: "62%", plugins: { legend: { position: "right" } } } });
    },
    combo(id, labels, barDs, lineDs) {
      return H._chart(id, { data: { labels, datasets: [
        { type: "bar", label: barDs.label, data: barDs.data, backgroundColor: "#c7d2fe", borderRadius: 4, yAxisID: "y" },
        { type: "line", label: lineDs.label, data: lineDs.data, borderColor: "#12b76a", backgroundColor: "#12b76a", tension: 0.3, yAxisID: "y1", spanGaps: true },
      ] }, options: { scales: { y: { beginAtZero: true, position: "left" }, y1: { beginAtZero: true, max: 100, position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + "%" } } } } });
    },

    // ------------------------------------------------------------ draft dialog
    openDraft(issueId, subject) {
      const dlg = $("#draftDialog");
      $("#draftIssueId").value = issueId || "";
      $("#draftIssueLabel").textContent = issueId ? `#${issueId} ${subject || ""}` : "a ticket";
      const sel = $("#draftTemplate");
      sel.innerHTML = '<option value="">(suggest automatically)</option>' +
        H.templates.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
      $("#draftPreview").hidden = true;
      H.renderDraftVars();
      dlg.showModal();
    },
    renderDraftVars() {
      const tpl = H.templates.find((t) => t.id === $("#draftTemplate").value);
      $("#draftTemplateInfo").textContent = tpl ? `Moves the ticket to "${tpl.target_status}"${tpl.description ? " - " + tpl.description : ""}` : "The template is chosen from closure.template_rules (by tracker) or the default template.";
      $("#draftVars").innerHTML = (tpl && tpl.variables ? tpl.variables : []).map((v) =>
        `<label class="field">${esc(v.label || v.name)}${v.required ? " *" : ""}
           <input data-var="${esc(v.name)}" value="${esc(v.default || "")}" ${v.required ? "required" : ""} placeholder="${esc(v.help || "")}"></label>`).join("");
    },
    _draftBody() {
      const vars = {};
      $$("#draftVars [data-var]").forEach((i) => { if (i.value.trim()) vars[i.dataset.var] = i.value; });
      return { issue_id: parseInt($("#draftIssueId").value, 10), template_id: $("#draftTemplate").value || null, variables: vars };
    },
    async previewDraft() {
      const b = H._draftBody();
      if (!b.issue_id) return H.toast("Enter a ticket number.", true);
      try {
        const tplId = b.template_id || (H.templates[0] && H.templates[0].id);
        const r = await H.api("POST", "/api/templates/preview", { template_id: tplId, issue_id: b.issue_id, variables: b.variables });
        const pre = $("#draftPreview");
        pre.textContent = r.note;
        pre.hidden = false;
      } catch (e) { H.toast(e.message, true); }
    },
    async submitDraft() {
      const b = H._draftBody();
      if (!b.issue_id) return H.toast("Enter a ticket number.", true);
      try {
        const p = await H.api("POST", "/api/proposals", b);
        $("#draftDialog").close();
        H.toast(`Proposal ${p.id} created for #${p.issue_id}. It is waiting for approval.`);
        setTimeout(() => (location.href = "/approvals#p-" + p.id), 900);
      } catch (e) { H.toast(e.message, true); }
    },

    // ------------------------------------------------------------ approvals
    async approve(pid, issueId) {
      try { H.requireReviewer(); } catch (e) { return; }
      const card = document.getElementById("p-" + pid);
      if (card && H._dirty(card)) {
        if (!confirm("You edited this proposal. Save the edits first, then approve?")) return;
        if (!(await H.saveProposal(pid, true))) return;
      }
      if (!confirm(`Approve as "${H.getReviewer()}" and close ticket #${issueId} in Redmine now?`)) return;
      try {
        const p = await H.api("POST", `/api/proposals/${pid}/approve`, { execute: true });
        H.toast(`#${p.issue_id}: ${(p.result && p.result.message) || p.status}`);
        setTimeout(() => location.reload(), 1100);
      } catch (e) { H.toast(e.message, true); setTimeout(() => location.reload(), 2500); }
    },
    _dirty(card) {
      return $$("textarea, input, select", card).some((el) => el.value !== el.defaultValue && !(el.tagName === "SELECT" && el.options[el.selectedIndex].defaultSelected));
    },
    async saveProposal(pid, silent) {
      try { H.requireReviewer(); } catch (e) { return false; }
      const card = document.getElementById("p-" + pid);
      const done = $(".p-done", card).value;
      try {
        await H.api("PUT", `/api/proposals/${pid}`, { note: $(".p-note", card).value, target_status: $(".p-status", card).value, done_ratio: done === "" ? null : parseInt(done, 10) });
        if (!silent) { H.toast("Saved. The proposal is ready for approval."); setTimeout(() => location.reload(), 800); }
        return true;
      } catch (e) { H.toast(e.message, true); return false; }
    },
    async reject(pid) {
      try { H.requireReviewer(); } catch (e) { return; }
      const reason = prompt("Reason for rejecting (optional):", "");
      if (reason === null) return;
      try { await H.api("POST", `/api/proposals/${pid}/reject`, { comment: reason }); H.toast("Rejected."); setTimeout(() => location.reload(), 700); }
      catch (e) { H.toast(e.message, true); }
    },
    async execute(pid, issueId) {
      try { H.requireReviewer(); } catch (e) { return; }
      if (!confirm(`Apply the approved change to ticket #${issueId} in Redmine now?`)) return;
      try { const p = await H.api("POST", `/api/proposals/${pid}/execute`); H.toast(`#${p.issue_id}: ${p.result.message}`); setTimeout(() => location.reload(), 1000); }
      catch (e) { H.toast(e.message, true); setTimeout(() => location.reload(), 2500); }
    },
    async cancel(pid) {
      if (!confirm("Cancel this proposal? Nothing will be changed in Redmine.")) return;
      try { await H.api("POST", `/api/proposals/${pid}/cancel`); location.reload(); } catch (e) { H.toast(e.message, true); }
    },
    async loadCandidates() {
      const box = $("#candidates");
      box.innerHTML = '<p class="muted small">Searching...</p>';
      try {
        const rows = await H.api("GET", "/api/candidates");
        if (!rows.length) { box.innerHTML = '<p class="empty">No closure candidates right now.</p>'; return; }
        box.innerHTML = `<table class="table compact"><thead><tr><th>#</th><th>Subject</th><th>Status</th><th>Assignee</th><th>Idle</th><th>Suggested template</th><th></th></tr></thead><tbody>${rows.map((r) => `
          <tr><td><a class="tid" href="${esc(r.url)}" target="_blank" rel="noopener">#${r.issue_id}</a></td><td class="subject">${esc(r.subject)}<div class="muted small">${esc(r.project)} &middot; ${esc(r.tracker)} &middot; ${esc(r.priority)}</div></td>
          <td>${esc(r.status)}</td><td>${esc(r.assigned_to)}</td><td>${r.idle_days}d</td><td>${esc(r.suggested_template || "-")}</td>
          <td><button class="btn small" data-id="${r.issue_id}" data-subject="${esc(r.subject)}" onclick="Harness.openDraft(${r.issue_id}, this.dataset.subject)">Draft</button></td></tr>`).join("")}</tbody></table>`;
      } catch (e) { box.innerHTML = `<p class="danger">${esc(e.message)}</p>`; }
    },
    async autoDraft() {
      if (!confirm("Draft closure proposals for every candidate? (Nothing is changed in Redmine - each one still needs approval.)")) return;
      try {
        const r = await H.api("POST", "/api/proposals/auto-draft");
        H.toast(`Drafted ${r.drafted} proposal(s)` + (r.skipped.length ? `, skipped ${r.skipped.length}: ${r.skipped.map((s) => "#" + s.issue_id + " " + s.reason).join("; ")}` : "."), r.skipped.length > 0 && !r.drafted);
        setTimeout(() => location.reload(), r.skipped.length ? 4000 : 1000);
      } catch (e) { H.toast(e.message, true); }
    },

    // ------------------------------------------------------------ reports
    renderColumns(selected) {
      const ds = $("#datasetSelect").value;
      const cols = H.reportColumns[ds] || [];
      const chosen = selected ? new Set(selected) : null;
      $("#columnsBox").innerHTML = cols.map((c) =>
        `<label title="${esc(c.key)}"><input type="checkbox" name="columns" value="${esc(c.key)}" ${(chosen ? chosen.has(c.key) : c.default) ? "checked" : ""}> ${esc(c.label)}</label>`).join("");
      $("#groupBy").innerHTML = cols.filter((c) => c.groupable).map((c) => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join("");
      $("#sortBy").innerHTML = '<option value="">(default)</option>' + cols.map((c) => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join("") +
        ["count", "open", "closed", "sla_breached", "sla_compliance_pct", "hours", "entries"].map((k) => `<option value="${k}">[group] ${k}</option>`).join("");
      const df = $("#dateField");
      const opts = ds === "time_entries" ? [["spent_on", "Date spent"]] : ds === "team" ? [["window", "Period"]] :
        [["created_on", "Created"], ["closed_on", "Closed"], ["updated_on", "Updated"], ["open_now", "Currently open (no date)"]];
      df.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
    },
    toggleCols(on) { $$("#columnsBox input").forEach((i) => (i.checked = on)); },
    applyPreset(key) {
      const p = H.reportPresets[key];
      if (!p) return;
      const f = $("#reportForm");
      f.dataset.value = p.dataset || "issues";
      $("#datasetSelect").value = p.dataset || "issues";
      H.renderColumns(p.columns);
      ["title", "days", "date_from", "date_to", "status", "project", "assignee", "tracker", "priority", "sla_state", "text"].forEach((k) => {
        if (f.elements[k]) f.elements[k].value = p[k] != null ? p[k] : (k === "days" ? 30 : "");
      });
      if (p.date_field) $("#dateField").value = p.date_field;
      $$("#groupBy option").forEach((o) => (o.selected = (p.group_by || []).includes(o.value)));
      if (p.sort_by) $("#sortBy").value = p.sort_by;
      f.elements.descending.checked = !!p.descending;
      H.previewReport();
    },
    _reportSpec() {
      const f = $("#reportForm");
      const spec = {};
      ["dataset", "title", "days", "date_field", "date_from", "date_to", "status", "project", "assignee", "tracker", "priority", "sla_state", "text", "sort_by"].forEach((k) => {
        const el = f.elements[k];
        if (el && el.value !== "") spec[k] = el.value;
      });
      spec.columns = $$("#columnsBox input:checked").map((i) => i.value);
      spec.group_by = $$("#groupBy option:checked").map((o) => o.value);
      spec.descending = f.elements.descending.checked;
      return spec;
    },
    async previewReport() {
      const box = $("#reportPreview");
      box.innerHTML = '<p class="muted small">Running report...</p>';
      try {
        const r = await H.api("POST", "/api/reports/preview", H._reportSpec());
        $("#reportTitle").textContent = r.title;
        $("#reportMeta").textContent = `${r.total_rows} row(s) - ${r.window} - generated ${r.generated_at}` + (r.total_rows > r.rows.length ? ` (showing first ${r.rows.length})` : "");
        if (!r.rows.length) { box.innerHTML = '<p class="empty">No rows match these filters.</p>'; return; }
        box.innerHTML = `<table class="table compact"><thead><tr>${r.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${r.rows.map((row) => `<tr>${row.map((v, i) => r.keys[i] === "url" ? `<td><a href="${esc(v)}" target="_blank" rel="noopener">open</a></td>` : `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      } catch (e) { box.innerHTML = `<p class="danger">${esc(e.message)}</p>`; }
    },
    downloadReport() {
      const spec = H._reportSpec();
      const params = new URLSearchParams();
      Object.entries(spec).forEach(([k, v]) => {
        if (Array.isArray(v)) v.forEach((x) => params.append(k, x));
        else if (v !== "" && v !== false && v != null) params.append(k, v);
      });
      location.href = "/export/report.csv?" + params.toString();
    },

    // ------------------------------------------------------------ templates
    addVarRow(v = {}) {
      const row = document.createElement("div");
      row.className = "var-row";
      row.innerHTML = `<input placeholder="name" data-k="name" value="${esc(v.name || "")}" pattern="[a-z_][a-z0-9_]*">
        <input placeholder="Label" data-k="label" value="${esc(v.label || "")}">
        <input placeholder="Default value" data-k="default" value="${esc(v.default || "")}">
        <label><input type="checkbox" data-k="required" ${v.required ? "checked" : ""}> req.</label>
        <button class="icon-btn" type="button" onclick="this.parentElement.remove()" aria-label="Remove">&times;</button>`;
      $("#varRows").appendChild(row);
    },
    addCfRow(k = "", v = "") {
      const row = document.createElement("div");
      row.className = "var-row cf";
      row.innerHTML = `<input placeholder="Custom field name (e.g. Resolution)" data-k="key" value="${esc(k)}">
        <input placeholder="Value (e.g. Fixed)" data-k="value" value="${esc(v)}">
        <button class="icon-btn" type="button" onclick="this.parentElement.remove()" aria-label="Remove">&times;</button>`;
      $("#cfRows").appendChild(row);
    },
    editTemplate(id) {
      const t = id ? H.templates.find((x) => x.id === id) : null;
      const f = $("#tplForm");
      f.reset();
      f.elements._mode.value = t ? "update" : "create";
      $("#tplEditorTitle").textContent = t ? `Edit "${t.name}"` : "New template";
      f.elements.id.value = t ? t.id : "";
      f.elements.id.readOnly = !!t;
      f.elements.name.value = t ? t.name : "";
      f.elements.description.value = t ? t.description || "" : "";
      if (t) f.elements.target_status.value = t.target_status;
      f.elements.done_ratio.value = t ? (t.done_ratio == null ? "" : t.done_ratio) : 100;
      f.elements.private_note.checked = !!(t && t.private_note);
      f.elements.note.value = t ? t.note : "Hello {{ issue.author }},\n\nTicket #{{ issue.id }} \"{{ issue.subject }}\" is now closed.\n\n{{ summary }}\n\nRegards,\n{{ me.name }}";
      $("#varRows").innerHTML = "";
      (t ? t.variables || [] : [{ name: "summary", label: "Summary", default: "The issue has been resolved." }]).forEach((v) => H.addVarRow(v));
      $("#cfRows").innerHTML = "";
      Object.entries((t && t.custom_fields) || {}).forEach(([k, v]) => H.addCfRow(k, v));
      const trackers = (t && t.applies_to && t.applies_to.trackers) || [];
      $$("input[name=tracker]", f).forEach((c) => (c.checked = trackers.includes(c.value)));
      $("#tplPreview").hidden = true;
      $("#reviewerHint").textContent = H.getReviewer() || "(set your reviewer name on the Approvals page)";
      $("#tplEditor").hidden = false;
      $("#tplEditor").scrollIntoView({ behavior: "smooth", block: "start" });
    },
    _collectTemplate() {
      const f = $("#tplForm");
      const variables = $$("#varRows .var-row").map((r) => ({
        name: $("[data-k=name]", r).value.trim(), label: $("[data-k=label]", r).value.trim(),
        default: $("[data-k=default]", r).value, required: $("[data-k=required]", r).checked,
      })).filter((v) => v.name);
      const custom_fields = {};
      $$("#cfRows .var-row").forEach((r) => { const k = $("[data-k=key]", r).value.trim(); if (k) custom_fields[k] = $("[data-k=value]", r).value; });
      const trackers = $$("input[name=tracker]:checked", f).map((c) => c.value);
      return {
        id: f.elements.id.value.trim(), name: f.elements.name.value.trim(), description: f.elements.description.value.trim(),
        target_status: f.elements.target_status.value, done_ratio: f.elements.done_ratio.value === "" ? null : parseInt(f.elements.done_ratio.value, 10),
        private_note: f.elements.private_note.checked, note: f.elements.note.value, variables, custom_fields,
        applies_to: trackers.length ? { trackers } : {},
      };
    },
    async saveTemplate() {
      if (!H.getReviewer()) return H.toast("Set your reviewer name on the Approvals page first.", true);
      const t = H._collectTemplate();
      const mode = $("#tplForm").elements._mode.value;
      try {
        if (mode === "update") await H.api("PUT", `/api/templates/${encodeURIComponent(t.id)}`, t);
        else await H.api("POST", "/api/templates", t);
        H.toast("Template saved.");
        setTimeout(() => location.reload(), 700);
      } catch (e) { H.toast(e.message, true); }
    },
    async deleteTemplate(id) {
      if (!H.getReviewer()) return H.toast("Set your reviewer name on the Approvals page first.", true);
      if (!confirm(`Delete template "${id}"? This cannot be undone.`)) return;
      try { await H.api("DELETE", `/api/templates/${encodeURIComponent(id)}`); H.toast("Deleted."); setTimeout(() => location.reload(), 600); }
      catch (e) { H.toast(e.message, true); }
    },
    async previewTemplate() {
      const t = H._collectTemplate();
      const vars = {};
      t.variables.forEach((v) => { vars[v.name] = v.default || `<${v.label || v.name}>`; });
      try {
        const r = await H.api("POST", "/api/templates/preview", { template: t, issue_id: $("#tplPreviewIssue").value || null, variables: vars });
        const pre = $("#tplPreview");
        pre.textContent = (r.problems && r.problems.length ? "PROBLEMS:\n- " + r.problems.join("\n- ") + "\n\n" : "") + `Preview for #${r.issue_id} ${r.issue_subject}\n\n` + r.note;
        pre.hidden = false;
      } catch (e) { H.toast(e.message, true); }
    },
  };
  window.Harness = H;
})();

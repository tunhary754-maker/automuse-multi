// AutoMuse Studio — frontend (không framework, fetch API thuần)
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---------------- tabs ----------------
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "outputs") loadOutputs();
    if (btn.dataset.tab === "build") loadJobs();
    if (btn.dataset.tab === "scripts") loadScripts();
    if (btn.dataset.tab === "create") loadBatches();
  });
});
function gotoTab(name) {
  document.querySelector(`.tab[data-tab="${name}"]`).click();
}

// ---------------- status / dashboard ----------------
async function loadStatus() {
  try {
    const s = await api("/api/status");
    const bf = $("#badge-ffmpeg");
    bf.textContent = s.ffmpeg ? "ffmpeg ✓" : "ffmpeg ✗";
    bf.className = "badge " + (s.ffmpeg ? "ok" : "bad");
    const ba = $("#badge-accounts");
    ba.textContent = `${s.accounts_ready}/${s.accounts} acc sẵn sàng`;
    ba.className = "badge " + (s.accounts_ready ? "ok" : "warn");
    $("#stat-cards").innerHTML = `
      <div class="stat"><div class="num">${s.accounts}</div><div class="label">Tài khoản Muse</div></div>
      <div class="stat"><div class="num">${s.outputs}</div><div class="label">Video đã dựng</div></div>
      <div class="stat"><div class="num">${s.ffmpeg ? "OK" : "Thiếu"}</div><div class="label">ffmpeg</div></div>`;
    const steps = [];
    if (!s.accounts_ready) steps.push("Thêm tài khoản Muse ở tab <b>Tài khoản Muse</b> (cookie + proxy).");
    steps.push("Mở tab <b>Tạo tập mới</b>: nhập chủ đề → Muse viết kịch bản → bấm <b>Tạo video</b>.");
    steps.push("Video build xong sẽ tự có tiếng gốc và file lời dẫn txt để bạn lồng tiếng sau.");
    if (s.outputs) steps.push(`Bạn đã có ${s.outputs} video trong tab <b>Kết quả</b>.`);
    $("#next-steps").innerHTML = steps.map((x) => `<li>${x}</li>`).join("");
  } catch (e) {
    $("#stat-cards").innerHTML = `<div class="stat">Không kết nối được server: ${esc(e.message)}</div>`;
  }
}

// ---------------- accounts ----------------
// ---------------- accounts: bảng (lọc trạng thái, tìm kiếm, tích chọn) ----------------
let accountsRows = [];
let accountsFilter = "all"; // all | ready | off
let accountsQuery = "";
let accountsSelected = new Set();
let accountsCheck = {}; // name -> html kết quả kiểm tra (giữ lại khi render lại)
function accountStatusKey(a) { return a.enabled ? "ready" : "off"; }
function accountsFiltered() {
  return accountsRows.filter((a) => {
    if (accountsFilter !== "all" && accountStatusKey(a) !== accountsFilter) return false;
    if (accountsQuery) {
      const hay = `${a.name} ${a.note || ""} ${a.proxy || ""}`.toLowerCase();
      if (!hay.includes(accountsQuery)) return false;
    }
    return true;
  });
}
function accountTableRow(a, stt) {
  const usage = a.usage
    ? `${a.usage.videos}/${a.usage.max_videos || "∞"} video · ${a.usage.minutes}/${a.usage.max_minutes || "∞"} phút<br><span class="meta">tuần ${esc(a.usage.week)}</span>`
    : "-";
  const checked = accountsSelected.has(a.name) ? "checked" : "";
  return `<tr>
    <td class="chk"><input type="checkbox" class="account-check" data-check="${esc(a.name)}" ${checked}></td>
    <td class="stt">${stt}</td>
    <td><div class="name-cell"><div class="thumb-ph">👤</div><div class="name-text"><div class="row-title">${esc(a.name)}</div><div class="row-sub">${esc(a.note || "—")}</div></div></div></td>
    <td>${esc(a.proxy || "—")}</td>
    <td>${a.jobs_done} ✓ / ${a.jobs_failed} ✗</td>
    <td>${usage}</td>
    <td>${!a.enabled ? `<span class="badge">tắt</span>` : `<span class="badge ok">sẵn sàng</span>`}
        ${a.error ? `<span class="meta">· lỗi: ${esc(a.error)}</span>` : ""}
        <span class="check-result">${accountsCheck[a.name] || ""}</span></td>
    <td><div class="row-actions">
      <button class="btn small" data-check-account="${esc(a.name)}">Kiểm tra</button>
      <button class="btn small danger" data-del-account="${esc(a.name)}">🗑 Xóa</button>
    </div></td>
  </tr>`;
}
function renderAccountsTable() {
  const rows = accountsFiltered();
  const counts = { all: accountsRows.length, ready: 0, off: 0 };
  for (const a of accountsRows) counts[accountStatusKey(a)]++;
  $("#accounts-pills").innerHTML = pillsHtml(counts, accountsFilter, [["all", "Tất cả"], ["ready", "Sẵn sàng"], ["off", "Tắt"]]);
  $("#accounts-tbody").innerHTML = rows.map((a, i) => accountTableRow(a, i + 1)).join("")
    || `<tr><td colspan="8" style="text-align:center;color:var(--muted)">Chưa có tài khoản nào.</td></tr>`;
  const visible = rows.map((a) => a.name);
  $("#accounts-check-all").checked = visible.length > 0 && visible.every((n) => accountsSelected.has(n));
}
async function loadAccounts() {
  try {
    accountsRows = await api("/api/accounts");
  } catch (e) {
    // API lỗi thì hiện lỗi ngay trong bảng, không để bảng trống im lặng.
    accountsRows = [];
    $("#accounts-pills").innerHTML = "";
    $("#accounts-tbody").innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--danger)">Không tải được tài khoản: ${esc(e.message)} — thử tải lại trang; nếu vẫn lỗi, kiểm tra lại acc mới thêm (proxy/cookie).</td></tr>`;
    return;
  }
  renderAccountsTable();
  const sel = $("#build-accounts-select");
  sel.innerHTML = accountsRows.filter((a) => a.enabled)
    .map((a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("");
  const csel = $("#create-account");
  csel.innerHTML = `<option value="">— danh sách mẫu (không cần acc) —</option>` +
    accountsRows.filter((a) => a.enabled)
      .map((a) => `<option value="${esc(a.name)}">${esc(a.name)} (nhờ Muse)</option>`).join("");
}
async function checkAccount(name) {
  accountsCheck[name] = " đang kiểm tra…";
  renderAccountsTable();
  try {
    const r = await api(`/api/accounts/${encodeURIComponent(name)}/check`, { method: "POST" });
    const parts = [];
    if (r.proxy) parts.push(`proxy ${r.proxy.ok ? "✓" : "✗"} (${esc(r.proxy.message)})`);
    parts.push(`login ${r.login.ok ? "✓" : "✗"} (${esc(r.login.message)})`);
    if (r.quota) parts.push(`token ${r.quota.ok === true ? "✓" : r.quota.ok === false ? "✗" : "?"} (${esc(r.quota.message)})`);
    if (r.usage) parts.push(`tuần này: ${r.usage.videos}/${r.usage.max_videos || "∞"} video · ${r.usage.minutes}/${r.usage.max_minutes || "∞"} phút`);
    accountsCheck[name] = parts.join("<br>");
  } catch (e) {
    accountsCheck[name] = " lỗi: " + esc(e.message);
  }
  renderAccountsTable();
}
async function deleteAccount(name) {
  if (!confirm(`Xóa tài khoản ${name}?`)) return;
  await api(`/api/accounts/${encodeURIComponent(name)}`, { method: "DELETE" });
  delete accountsCheck[name];
  accountsSelected.delete(name);
  loadAccounts(); loadStatus();
}
$("#accounts-pills").addEventListener("click", (e) => {
  const pill = e.target.closest("[data-pill]");
  if (!pill) return;
  accountsFilter = pill.dataset.pill;
  renderAccountsTable();
});
$("#accounts-search").addEventListener("input", (e) => {
  accountsQuery = e.target.value.trim().toLowerCase();
  renderAccountsTable();
});
$("#accounts-check-all").addEventListener("change", (e) => {
  const on = e.target.checked;
  for (const a of accountsFiltered()) {
    if (on) accountsSelected.add(a.name); else accountsSelected.delete(a.name);
  }
  renderAccountsTable();
});
$("#btn-accounts-select-all").addEventListener("click", () => {
  const rows = accountsFiltered();
  const allChecked = rows.length && rows.every((a) => accountsSelected.has(a.name));
  for (const a of rows) {
    if (allChecked) accountsSelected.delete(a.name); else accountsSelected.add(a.name);
  }
  renderAccountsTable();
});
$("#accounts-tbody").addEventListener("click", (e) => {
  const check = e.target.closest(".account-check");
  if (check) {
    const n = check.dataset.check;
    if (check.checked) accountsSelected.add(n); else accountsSelected.delete(n);
    renderAccountsTable();
    return;
  }
  const chk = e.target.closest("[data-check-account]");
  if (chk) { checkAccount(chk.dataset.checkAccount); return; }
  const del = e.target.closest("[data-del-account]");
  if (del) { deleteAccount(del.dataset.delAccount); return; }
});
$("#btn-accounts-check-selected").addEventListener("click", async () => {
  const rows = accountsFiltered().filter((a) => accountsSelected.has(a.name));
  if (!rows.length) return alert("Chưa tích chọn tài khoản nào.");
  const msg = $("#accounts-msg");
  msg.className = "msg"; msg.textContent = `Đang kiểm tra ${rows.length} tài khoản…`;
  for (const a of rows) {
    try { await checkAccount(a.name); } catch (e) { /* kết quả ghi vào bảng */ }
  }
  msg.className = "msg ok"; msg.textContent = `Đã kiểm tra ${rows.length} tài khoản — xem kết quả trong cột Trạng thái.`;
});
$("#btn-accounts-del-selected").addEventListener("click", async () => {
  const rows = accountsFiltered().filter((a) => accountsSelected.has(a.name));
  if (!rows.length) return alert("Chưa tích chọn tài khoản nào.");
  if (!confirm(`Xóa ${rows.length} tài khoản?`)) return;
  let ok = 0;
  for (const a of rows) {
    try {
      await api(`/api/accounts/${encodeURIComponent(a.name)}`, { method: "DELETE" });
      delete accountsCheck[a.name];
      ok++;
    } catch (e) { /* báo tổng cuối */ }
  }
  accountsSelected.clear();
  const msg = $("#accounts-msg");
  msg.className = "msg ok"; msg.textContent = `Đã xóa ${ok}/${rows.length} tài khoản.`;
  loadAccounts(); loadStatus();
});
$("#account-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const msg = $("#account-msg");
  msg.className = "msg"; msg.textContent = "Đang thêm…";
  try {
    await api("/api/accounts", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(fd)),
    });
    msg.className = "msg ok"; msg.textContent = "Đã thêm tài khoản.";
    e.target.reset(); loadAccounts(); loadStatus();
  } catch (err) {
    msg.className = "msg err"; msg.textContent = err.message;
  }
});

$("#notify-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const msg = $("#notify-msg");
  msg.className = "msg"; msg.textContent = "Đang kiểm tra token và gửi tin thử…";
  try {
    const r = await api("/api/notify/save", {
      method: "POST",
      body: JSON.stringify({ token: fd.get("token"), chat_id: fd.get("chat_id") }),
    });
    msg.className = "msg ok";
    msg.textContent = `Đã lưu Telegram (bot ${r.bot}) — tin thử đã tới. Khi build hết acc, bạn sẽ nhận tin nhắn để thêm acc mới.`;
  } catch (err) {
    msg.className = "msg err"; msg.textContent = err.message;
  }
});

$("#btn-notify-test").addEventListener("click", async () => {
  const msg = $("#notify-msg");
  msg.className = "msg"; msg.textContent = "Đang gửi tin thử…";
  try {
    await api("/api/notify/test", { method: "POST", body: "{}" });
    msg.className = "msg ok"; msg.textContent = "Đã gửi tin thử.";
  } catch (err) {
    msg.className = "msg err"; msg.textContent = err.message;
  }
});

// ---------------- scripts ----------------
// Tool chỉ làm phim tài liệu: chỉ liệt kê kịch bản documentary (kèm file
// lỗi chưa xác định được kind, để người dùng vẫn thấy và sửa được).
// Danh sách gom theo chủ đề: kịch bản thuộc đợt hàng loạt nào thì nằm
// dưới chủ đề đó (kèm tiến độ video từng kịch bản); còn lại vào nhóm
// "Kịch bản khác".
let currentScriptPath = null;
let editorCanDelete = false;
function syncEditorDelBtn() {
  const btn = $("#btn-del-script");
  if (btn) btn.style.display = editorCanDelete ? "" : "none";
}
function statusBadge(p) {
  if (!p) return `<span class="badge">Chưa dựng</span>`;
  if (p.status === "done") return `<span class="badge ok">Đã dựng xong</span>`;
  if (p.status === "building") return `<span class="badge running">Đang dựng ${p.pct}%</span>`;
  return `<span class="badge">Chưa dựng</span>`;
}
// ---------------- scripts: bảng (lọc trạng thái, tìm kiếm, chủ đề, tích chọn) ----------------
let scriptsRows = [];
let scriptsFilter = "all"; // all | waiting | running | done
let scriptsQuery = "";
let scriptsTopic = "";
let scriptsSelected = new Set();
function scriptStatusKey(p) {
  if (!p) return "waiting";
  if (p.status === "done") return "done";
  if (p.status === "building") return "running";
  return "waiting";
}
function pillsHtml(counts, active, defs) {
  // defs: [[key, label], ...] — hiện đủ các pill kể cả khi đếm 0 (như mẫu).
  return defs.map(([k, label]) => `<button class="pill ${k === active ? "active" : ""}" data-pill="${k}">${label} <span class="n">${counts[k] ?? 0}</span></button>`).join("");
}
function scriptsFiltered() {
  return scriptsRows.filter((s) => {
    if (scriptsFilter !== "all" && scriptStatusKey(s.progress) !== scriptsFilter) return false;
    if (scriptsTopic && (s.topic || "") !== scriptsTopic) return false;
    if (scriptsQuery) {
      const hay = `${s.name} ${s.title || ""} ${s.topic || ""} ${s.snippet || ""}`.toLowerCase();
      if (!hay.includes(scriptsQuery)) return false;
    }
    return true;
  });
}
function scriptTableRow(s, stt) {
  const p = s.progress;
  const clips = p && p.status === "building" ? ` · ${p.clips}/${p.scenes} clip` : "";
  const out = p && p.output_path ? ` · 📁 ${esc(p.output_path)}` : "";
  // Chỉ xóa được kịch bản trong scripts/ (mẫu template/example là đồ
  // chung, endpoint xóa cũng chỉ làm việc trong scripts/).
  const del = s.source === "scripts"
    ? `<button class="btn small danger" data-del-script="${esc(s.name)}">🗑 Xóa</button>` : "";
  const checked = scriptsSelected.has(s.path) ? "checked" : "";
  return `<tr data-path="${esc(s.path)}">
    <td class="chk"><input type="checkbox" class="script-check" data-check="${esc(s.path)}" ${checked}></td>
    <td class="stt">${stt}</td>
    <td><div class="name-cell"><div class="thumb-ph">📄</div><div class="name-text"><div class="row-title">${esc(s.title || s.name)}</div><div class="row-sub">${esc(s.snippet || `${s.source} · ${s.kind} · ${s.scenes} cảnh`)}</div></div></div></td>
    <td>${esc(s.topic || "—")}</td>
    <td>${esc(s.kind)}</td>
    <td>${esc(s.account || "—")}</td>
    <td>${statusBadge(p)}<span class="meta">${clips}${out}</span></td>
    <td><div class="row-actions">
      <button class="btn small" data-open-script="${esc(s.path)}">Mở</button>
      <button class="btn small" data-build-script="${esc(s.path)}">▶ Video</button>
      ${del}
    </div></td>
  </tr>`;
}
function renderScriptsTable() {
  const rows = scriptsFiltered();
  const counts = { all: scriptsRows.length, waiting: 0, running: 0, done: 0 };
  for (const s of scriptsRows) counts[scriptStatusKey(s.progress)]++;
  $("#scripts-pills").innerHTML = pillsHtml(counts, scriptsFilter, [["all", "Tất cả"], ["waiting", "Chờ"], ["running", "Đang chạy"], ["done", "Xong"]]);
  $("#scripts-tbody").innerHTML = rows.map((s, i) => scriptTableRow(s, i + 1)).join("")
    || `<tr><td colspan="8" style="text-align:center;color:var(--muted)">Chưa có kịch bản nào khớp.</td></tr>`;
  const visible = rows.map((s) => s.path);
  $("#scripts-check-all").checked = visible.length > 0 && visible.every((p) => scriptsSelected.has(p));
}
async function loadScripts() {
  const all = await api("/api/scripts");
  const list = all.filter((s) => s.kind === "documentary" || s.kind === "?");
  let batches = [];
  try { batches = await api("/api/batches"); } catch (e) { batches = []; }
  // Map đường dẫn kịch bản -> chủ đề + tài khoản đã viết (đợt mới nhất
  // thắng, vì /api/batches trả mới nhất trước).
  const topicOf = new Map();
  const accountOf = new Map();
  for (const b of batches) {
    for (const t of b.topics || []) {
      for (const s of t.scripts || []) {
        if (!s.file) continue;
        if (!topicOf.has(s.file)) topicOf.set(s.file, t.name);
        if (!accountOf.has(s.file) && s.account) accountOf.set(s.file, s.account);
      }
    }
  }
  scriptsRows = list.map((s) => ({
    ...s,
    topic: topicOf.get(s.path) || s.topic || "",
    account: accountOf.get(s.path) || "",
  }));
  // Lọc chủ đề: giữ lựa chọn cũ nếu còn, không thì về Tất cả.
  const topics = [...new Set(scriptsRows.map((s) => s.topic).filter(Boolean))].sort();
  const topicSel = $("#scripts-topic-filter");
  const prevTopic = topicSel.value;
  topicSel.innerHTML = `<option value="">Tất cả chủ đề</option>` + topics.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  topicSel.value = topics.includes(prevTopic) ? prevTopic : "";
  scriptsTopic = topicSel.value;
  renderScriptsTable();
  const sel = $("#build-script-select");
  const prev = sel.value;
  sel.innerHTML = list.map((s) => `<option value="${esc(s.path)}">${esc(s.name)} (${esc(s.source)} · ${s.scenes} cảnh)</option>`).join("");
  if (prev) sel.value = prev;
}
$("#scripts-pills").addEventListener("click", (e) => {
  const pill = e.target.closest("[data-pill]");
  if (!pill) return;
  scriptsFilter = pill.dataset.pill;
  renderScriptsTable();
});
$("#scripts-search").addEventListener("input", (e) => {
  scriptsQuery = e.target.value.trim().toLowerCase();
  renderScriptsTable();
});
$("#scripts-topic-filter").addEventListener("change", (e) => {
  scriptsTopic = e.target.value;
  renderScriptsTable();
});
$("#scripts-check-all").addEventListener("change", (e) => {
  const on = e.target.checked;
  for (const s of scriptsFiltered()) {
    if (on) scriptsSelected.add(s.path); else scriptsSelected.delete(s.path);
  }
  renderScriptsTable();
});
$("#btn-scripts-select-all").addEventListener("click", () => {
  const rows = scriptsFiltered();
  const allChecked = rows.length && rows.every((s) => scriptsSelected.has(s.path));
  for (const s of rows) {
    if (allChecked) scriptsSelected.delete(s.path); else scriptsSelected.add(s.path);
  }
  renderScriptsTable();
});
$("#scripts-tbody").addEventListener("click", async (e) => {
  const check = e.target.closest(".script-check");
  if (check) {
    const p = check.dataset.check;
    if (check.checked) scriptsSelected.add(p); else scriptsSelected.delete(p);
    renderScriptsTable();
    return;
  }
  const del = e.target.closest("[data-del-script]");
  if (del) {
    if (!confirm(`Xóa kịch bản "${del.dataset.delScript}"? (xóa cả file prompts + lời dẫn kèm theo)`)) return;
    try {
      await api(`/api/scripts/${encodeURIComponent(del.dataset.delScript)}`, { method: "DELETE" });
      loadScripts();
    } catch (err) { alert(err.message); }
    return;
  }
  const build = e.target.closest("[data-build-script]");
  if (build) {
    gotoTab("build");
    $("#build-script-select").value = build.dataset.buildScript;
    return;
  }
  const open = e.target.closest("[data-open-script]");
  if (open) { openScript(open.dataset.openScript); return; }
  const tr = e.target.closest("tr[data-path]");
  if (tr) openScript(tr.dataset.path);
});
$("#btn-scripts-del-selected").addEventListener("click", async () => {
  const rows = scriptsFiltered().filter((s) => scriptsSelected.has(s.path));
  if (!rows.length) return alert("Chưa tích chọn kịch bản nào.");
  const deletable = rows.filter((s) => s.source === "scripts");
  if (!deletable.length) return alert("Các kịch bản đã chọn đều là mẫu (template/example) nên không xóa được.");
  if (!confirm(`Xóa ${deletable.length} kịch bản? (xóa cả file prompts + lời dẫn kèm theo)`)) return;
  let ok = 0;
  for (const s of deletable) {
    try { await api(`/api/scripts/${encodeURIComponent(s.name)}`, { method: "DELETE" }); ok++; }
    catch (e) { /* báo tổng cuối */ }
  }
  scriptsSelected.clear();
  const msg = $("#scripts-msg");
  msg.className = "msg ok";
  msg.textContent = `Đã xóa ${ok}/${deletable.length} kịch bản${rows.length - deletable.length ? ` (bỏ qua ${rows.length - deletable.length} mẫu)` : ""}.`;
  loadScripts();
});
$("#btn-scripts-build-selected").addEventListener("click", async () => {
  const rows = scriptsFiltered().filter((s) => scriptsSelected.has(s.path));
  if (!rows.length) return alert("Chưa tích chọn kịch bản nào.");
  const msg = $("#scripts-msg");
  msg.className = "msg"; msg.textContent = `Đang xếp hàng ${rows.length} video…`;
  let ok = 0;
  for (const s of rows) {
    try {
      await api("/api/build", { method: "POST", body: JSON.stringify({ script: s.path }) });
      ok++;
    } catch (e) { /* job lỗi thì đếm vào phần chưa xếp được */ }
  }
  scriptsSelected.clear();
  msg.className = "msg ok";
  msg.textContent = `Đã xếp hàng ${ok}/${rows.length} video — xem tiến độ ở tab Tạo video.`;
  loadJobs();
});
async function openScript(path) {
  const data = await api(`/api/scripts/content?path=${encodeURIComponent(path)}`);
  currentScriptPath = path;
  editorCanDelete = path.startsWith("scripts/");
  syncEditorDelBtn();
  $("#editor-title").textContent = path;
  $("#script-editor").value = data.content;
  loadScripts();
  // Bảng giờ nằm phía trên, khung soạn ở dưới — cuộn xuống khung soạn.
  document.querySelector("#tab-scripts .editor")?.scrollIntoView({ behavior: "smooth", block: "start" });
}
$("#btn-new-script").addEventListener("click", () => {
  const name = prompt("Tên kịch bản mới (vd: ca-voi-xanh):");
  if (!name) return;
  currentScriptPath = `scripts/${name}.yaml`;
  editorCanDelete = false;
  syncEditorDelBtn();
  $("#editor-title").textContent = currentScriptPath + " (mới)";
  $("#script-editor").value = `title: "${name}"
kind: documentary
aspect: "16:9"
default_voice: narrator
fps: 30
scenes:
  - id: s01
    title: "Cảnh 1"
    duration: 10
    visual: "Describe the scene in English for Muse video generation..."
    narration: "Lời dẫn tiếng Việt ở đây, một cơ chế sinh tồn và một con số."
`;
});
async function loadTemplate(kind) {
  const path = `templates/${kind}_script.yaml`;
  await openScript(path);
  currentScriptPath = null; // template chỉ để xem, lưu sẽ hỏi tên mới
  editorCanDelete = false;
  syncEditorDelBtn();
  $("#editor-title").textContent = path + " (mẫu — bấm Lưu để tạo bản sao)";
}
$("#btn-del-script").addEventListener("click", async () => {
  if (!currentScriptPath || !editorCanDelete) return;
  const name = currentScriptPath.replace(/^scripts\//, "").replace(/\.ya?ml$/, "");
  if (!confirm(`Xóa kịch bản "${name}"? (xóa cả file prompts + lời dẫn kèm theo)`)) return;
  try {
    await api(`/api/scripts/${encodeURIComponent(name)}`, { method: "DELETE" });
    currentScriptPath = null;
    editorCanDelete = false;
    syncEditorDelBtn();
    $("#editor-title").textContent = "Chưa chọn kịch bản";
    $("#script-editor").value = "";
    $("#review-changes").innerHTML = "";
    loadScripts();
  } catch (err) { alert(err.message); }
});
$("#btn-template-doc").addEventListener("click", () => loadTemplate("documentary"));
$("#btn-save-script").addEventListener("click", async () => {
  const msg = $("#script-msg");
  let name = currentScriptPath
    ? currentScriptPath.replace(/^scripts\//, "").replace(/\.ya?ml$/, "")
    : null;
  if (!name || currentScriptPath.startsWith("templates/") || currentScriptPath.startsWith("examples/")) {
    name = prompt("Lưu thành tên nào? (vd: phim-cua-toi)", name || "");
    if (!name) return;
  }
  try {
    const r = await api("/api/scripts", {
      method: "POST",
      body: JSON.stringify({ name, content: $("#script-editor").value }),
    });
    msg.className = "msg ok"; msg.textContent = "Đã lưu: " + r.path;
    currentScriptPath = r.path; editorCanDelete = true; syncEditorDelBtn(); loadScripts();
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});
$("#btn-build-script").addEventListener("click", () => {
  if (!currentScriptPath) return alert("Chọn kịch bản trước đã.");
  gotoTab("build");
  $("#build-script-select").value = currentScriptPath;
});
$("#btn-review-script").addEventListener("click", async () => {
  const content = $("#script-editor").value;
  const msg = $("#review-msg");
  if (!content.trim()) return alert("Chưa có nội dung kịch bản để rà soát.");
  const instruction = $("#script-review-instruction").value.trim();
  msg.className = "msg";
  msg.textContent = "Muse đang đọc lại kịch bản, sửa phần chưa tốt và bổ sung theo yêu cầu… (có thể mất 1–2 phút)";
  $("#review-changes").innerHTML = "";
  try {
    const r = await api("/api/scripts/review", {
      method: "POST",
      body: JSON.stringify({ content, instruction }),
    });
    $("#script-editor").value = r.fixed_content;
    const items = r.changes.length
      ? r.changes.map((c) => `<li>${esc(c)}</li>`).join("")
      : "<li>Không cần sửa gì — kịch bản đã tốt.</li>";
    $("#review-changes").innerHTML = `<p><b>Đã rà soát (${r.scenes_before} → ${r.scenes_after} cảnh):</b></p><ul class="list">${items}</ul><p class="hint">Bản sửa đã nạp vào khung soạn — bạn xem lại rồi bấm Lưu để lưu.</p>`;
    msg.className = "msg ok";
    msg.textContent = `Đã rà soát xong: ${r.changes.length} chỗ sửa.`;
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});

// ---------------- build ----------------
let pollTimer = null;
$("#build-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const msg = $("#build-msg");
  const body = {
    script: fd.get("script"),
    mock: fd.get("mock") === "on",
    no_tts: true, // phim tài liệu: luôn không lồng tiếng, lời dẫn xuất txt riêng
    no_iris: fd.get("no_iris") === "on",
    resume: fd.get("resume") === "on",
    out: fd.get("out") || "",
    accounts: [...$("#build-accounts-select").selectedOptions].map((o) => o.value),
  };
  msg.className = "msg"; msg.textContent = "Đang gửi job…";
  try {
    const r = await api("/api/build", { method: "POST", body: JSON.stringify(body) });
    msg.className = "msg ok"; msg.textContent = "Job đã chạy: " + r.job_id;
    watchJob(r.job_id);
  } catch (err) {
    msg.className = "msg err"; msg.textContent = err.message;
  }
});
async function watchJob(jobId) {
  clearInterval(pollTimer);
  const badge = $("#job-status");
  const logEl = $("#job-log");
  const bar = $("#progress-bar");
  badge.className = "badge running"; badge.textContent = "đang chạy…";
  logEl.textContent = "";
  pollTimer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      logEl.textContent = job.log.join("\n");
      logEl.scrollTop = logEl.scrollHeight;
      const total = (job.manifest && job.manifest.scenes) ? job.manifest.scenes.length : 0;
      const done = (job.log.join("\n").match(/Chuẩn hóa /g) || []).length;
      bar.style.width = total ? Math.min(100, (done / total) * 100) + "%" : "15%";
      $("#job-meta").textContent = `${esc(job.script)} · ${job.started_at || ""}`;
      badge.className = "badge " + (job.status === "running" ? "running" : job.status === "queued" ? "" : job.status === "done" ? "ok" : job.status === "failed" ? "bad" : "");
      badge.textContent = job.status === "running" ? "đang chạy…" : job.status === "queued" ? "đang chờ…" : job.status === "done" ? "xong ✓" : job.status === "failed" ? "lỗi ✗" : job.status === "cancelled" ? "đã hủy" : job.status;
      if (job.status === "done" || job.status === "failed" || job.status === "cancelled") {
        clearInterval(pollTimer);
        if (job.status === "done") bar.style.width = "100%";
        if (job.error) logEl.textContent += "\nLỖI: " + job.error;
        loadJobs(); loadOutputs(); loadStatus();
      }
    } catch (e) { /* server mất kết nối tạm thời thì poll tiếp */ }
  }, 2000);
}
async function loadJobs() {
  jobsRows = await api("/api/jobs");
  renderJobsTable();
}
// ---------------- jobs: bảng (lọc trạng thái, tìm kiếm, tích chọn) ----------------
let jobsRows = [];
let jobsFilter = "all"; // all | waiting | running | done | failed | cancelled
let jobsQuery = "";
let jobsSelected = new Set();
function jobStatusKey(j) {
  if (j.status === "done") return "done";
  if (j.status === "running") return "running";
  if (j.status === "failed") return "failed";
  if (j.status === "cancelled") return "cancelled";
  return "waiting"; // queued
}
function jobBadge(j) {
  const k = jobStatusKey(j);
  if (k === "done") return `<span class="badge ok">Xong</span>`;
  if (k === "running") return `<span class="badge running">Đang chạy</span>`;
  if (k === "failed") return `<span class="badge bad">Lỗi</span>`;
  if (k === "cancelled") return `<span class="badge">Đã hủy</span>`;
  return `<span class="badge">Chờ</span>`;
}
function scriptTitleFor(path) {
  const s = scriptsRows.find((x) => x.path === path);
  return s ? (s.title || s.name) : "";
}
function jobsFiltered() {
  return jobsRows.filter((j) => {
    if (jobsFilter !== "all" && jobStatusKey(j) !== jobsFilter) return false;
    if (jobsQuery) {
      const hay = `${j.id} ${j.script} ${scriptTitleFor(j.script)}`.toLowerCase();
      if (!hay.includes(jobsQuery)) return false;
    }
    return true;
  });
}
function jobTableRow(j, stt) {
  const title = scriptTitleFor(j.script) || j.script;
  const accounts = (j.accounts && j.accounts.length) ? j.accounts.join(", ") : "tất cả";
  const checked = jobsSelected.has(j.id) ? "checked" : "";
  const err = j.error ? ` <span class="meta">· ${esc(j.error)}</span>` : "";
  let actions = `<button class="btn small" data-watch-job="${esc(j.id)}">Xem log</button>`;
  if (j.status === "queued") actions += ` <button class="btn small" data-cancel-job="${esc(j.id)}">⏹ Hủy</button>`;
  if (j.status === "done" || j.status === "failed" || j.status === "cancelled") actions += ` <button class="btn small" data-retry-job="${esc(j.script)}">↻ Chạy lại</button>`;
  if (j.status !== "running") actions += ` <button class="btn small danger" data-del-job="${esc(j.id)}">🗑 Xóa</button>`;
  return `<tr>
    <td class="chk"><input type="checkbox" class="job-check" data-check="${esc(j.id)}" ${checked}></td>
    <td class="stt">${stt}</td>
    <td><div class="name-cell"><div class="thumb-ph">🎬</div><div class="name-text"><div class="row-title">${esc(title)}</div><div class="row-sub">${esc(j.script)} · ${esc(j.id)}${j.mock ? " · mock" : ""}</div></div></div></td>
    <td>Video</td>
    <td>${esc(accounts)}</td>
    <td>${jobBadge(j)}${err}</td>
    <td><div class="row-actions">${actions}</div></td>
  </tr>`;
}
function renderJobsTable() {
  const rows = jobsFiltered();
  const counts = { all: jobsRows.length, waiting: 0, running: 0, done: 0, failed: 0, cancelled: 0 };
  for (const j of jobsRows) counts[jobStatusKey(j)]++;
  $("#jobs-pills").innerHTML = pillsHtml(counts, jobsFilter, [["all", "Tất cả"], ["waiting", "Chờ"], ["running", "Đang chạy"], ["done", "Xong"], ["failed", "Lỗi"], ["cancelled", "Đã hủy"]]);
  $("#jobs-tbody").innerHTML = rows.map((j, i) => jobTableRow(j, i + 1)).join("")
    || `<tr><td colspan="7" style="text-align:center;color:var(--muted)">Chưa có job nào.</td></tr>`;
  const visible = rows.map((j) => j.id);
  $("#jobs-check-all").checked = visible.length > 0 && visible.every((id) => jobsSelected.has(id));
}
$("#jobs-pills").addEventListener("click", (e) => {
  const pill = e.target.closest("[data-pill]");
  if (!pill) return;
  jobsFilter = pill.dataset.pill;
  renderJobsTable();
});
$("#jobs-search").addEventListener("input", (e) => {
  jobsQuery = e.target.value.trim().toLowerCase();
  renderJobsTable();
});
$("#jobs-check-all").addEventListener("change", (e) => {
  const on = e.target.checked;
  for (const j of jobsFiltered()) {
    if (on) jobsSelected.add(j.id); else jobsSelected.delete(j.id);
  }
  renderJobsTable();
});
$("#btn-jobs-select-all").addEventListener("click", () => {
  const rows = jobsFiltered();
  const allChecked = rows.length && rows.every((j) => jobsSelected.has(j.id));
  for (const j of rows) {
    if (allChecked) jobsSelected.delete(j.id); else jobsSelected.add(j.id);
  }
  renderJobsTable();
});
$("#jobs-tbody").addEventListener("click", async (e) => {
  const check = e.target.closest(".job-check");
  if (check) {
    const id = check.dataset.check;
    if (check.checked) jobsSelected.add(id); else jobsSelected.delete(id);
    renderJobsTable();
    return;
  }
  const watch = e.target.closest("[data-watch-job]");
  if (watch) { watchJob(watch.dataset.watchJob); return; }
  const cancel = e.target.closest("[data-cancel-job]");
  if (cancel) {
    try { await api(`/api/jobs/${encodeURIComponent(cancel.dataset.cancelJob)}/cancel`, { method: "POST" }); loadJobs(); }
    catch (err) { alert(err.message); }
    return;
  }
  const retry = e.target.closest("[data-retry-job]");
  if (retry) {
    try {
      const r = await api("/api/build", { method: "POST", body: JSON.stringify({ script: retry.dataset.retryJob }) });
      watchJob(r.job_id);
    } catch (err) { alert(err.message); }
    return;
  }
  const del = e.target.closest("[data-del-job]");
  if (del) {
    try { await api(`/api/jobs/${encodeURIComponent(del.dataset.delJob)}`, { method: "DELETE" }); loadJobs(); }
    catch (err) { alert(err.message); }
    return;
  }
});
$("#btn-jobs-retry-selected").addEventListener("click", async () => {
  const rows = jobsFiltered().filter((j) => jobsSelected.has(j.id));
  if (!rows.length) return alert("Chưa tích chọn job nào.");
  const msg = $("#jobs-msg");
  msg.className = "msg"; msg.textContent = `Đang xếp hàng ${rows.length} video…`;
  let ok = 0;
  for (const j of rows) {
    try { await api("/api/build", { method: "POST", body: JSON.stringify({ script: j.script }) }); ok++; }
    catch (e) { /* đếm cuối */ }
  }
  jobsSelected.clear();
  msg.className = "msg ok"; msg.textContent = `Đã xếp hàng ${ok}/${rows.length} video.`;
  loadJobs();
});
$("#btn-jobs-del-selected").addEventListener("click", async () => {
  const rows = jobsFiltered().filter((j) => jobsSelected.has(j.id));
  if (!rows.length) return alert("Chưa tích chọn job nào.");
  const deletable = rows.filter((j) => j.status !== "running");
  if (!deletable.length) return alert("Các job đã chọn đều đang chạy nên không xóa được.");
  let ok = 0;
  for (const j of deletable) {
    try { await api(`/api/jobs/${encodeURIComponent(j.id)}`, { method: "DELETE" }); ok++; }
    catch (e) { /* đếm cuối */ }
  }
  jobsSelected.clear();
  const msg = $("#jobs-msg");
  msg.className = "msg ok";
  msg.textContent = `Đã xóa ${ok}/${deletable.length} job${rows.length - deletable.length ? ` (bỏ qua ${rows.length - deletable.length} job đang chạy)` : ""}.`;
  loadJobs();
});

// ---------------- create: topics + write-script (phim tài liệu) ----------------
let createdScriptPath = null;
$("#btn-auto-create").addEventListener("click", async () => {
  const msg = $("#auto-msg");
  const minutes = parseInt($("#auto-minutes").value, 10) || 0;
  msg.className = "msg"; msg.textContent = "Đang chọn chủ đề, nghiên cứu rồi viết kịch bản dài… (có thể mất 2–4 phút)";
  try {
    const r = await api("/api/auto-create", { method: "POST", body: JSON.stringify({ minutes }) });
    createdScriptPath = r.path;
    msg.className = "msg ok";
    let text = `Đã tạo: ${r.title} — chủ đề “${r.topic}”, ${r.scenes} cảnh, tổng ${r.total_duration}s (~${Math.round(r.total_duration / 60)} phút, mục tiêu ${r.minutes_target || "tự quyết"} phút, acc ${r.account}).`;
    if (r.account_warnings && r.account_warnings.length) {
      text += " ⚠️ " + r.account_warnings.map((w) => `Acc ${w.account} KHÔNG KẾT NỐI ĐƯỢC (${w.error}) — đã dùng acc khác thay thế.`).join(" ");
    }
    msg.textContent = text;
    $("#create-result").style.display = "";
    $("#create-result-title").textContent = `${r.title} — ${r.path}`;
    const content = await api(`/api/scripts/content?path=${encodeURIComponent(r.path)}`);
    $("#create-result-script").textContent = content.content;
    loadScripts();
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});

// ---------------- batch: sản xuất hàng loạt theo chủ đề ----------------
let batchPollTimer = null;
function batchStatusLabel(s) {
  return { running: "⏳ Đang chạy", paused: "⏸ Tạm dừng", done: "✅ Xong",
           partial: "⚠️ Xong 1 phần (có lỗi)", failed: "❌ Lỗi",
           cancelled: "⏹ Đã dừng hẳn",
           interrupted: "⏸ Gián đoạn (server restart/crash)" }[s] || s;
}
function renderBatches(batches) {
  if (!batches.length) {
    $("#batches-list").innerHTML = "Chưa có đợt nào. Nhập số chủ đề/số tập/số phút rồi bấm “Tạo hàng loạt kịch bản”.";
    return;
  }
  $("#batches-list").innerHTML = batches.map((b) => {
    const p = b.progress || { done: 0, failed: 0, total: 0, pct: 0 };
    const topicsHtml = (b.topics || []).map((t) => {
      const done = (t.scripts || []).filter((s) => s.status === "done").length;
      const rows = (t.scripts || []).map((s) => {
        const icon = s.status === "done" ? "✅" : s.status === "failed" ? "❌" : s.status === "writing" ? "✍️" : s.status === "cancelled" ? "⏹" : "⏳";
        const detail = s.status === "done" ? `${esc(s.title)} (${s.scenes} cảnh)`
          : s.status === "failed" ? `Lỗi: ${esc(s.error)}` : s.status === "writing" ? "đang viết…" : s.status === "cancelled" ? "đã dừng" : "chờ viết";
        return `<li>${icon} Tập ${s.index}: ${detail}</li>`;
      }).join("");
      return `<div class="batch-topic"><b>📁 ${esc(t.name)}</b> — ${done}/${(t.scripts || []).length} kịch bản
        ${t.reason ? `<span class="meta"> · ${esc(t.reason)}</span>` : ""}
        <ul class="list">${rows}</ul></div>`;
    }).join("");
    return `<div class="card batch-card">
      <div class="row between"><strong>${esc(b.id)}</strong>
        <span class="badge">${batchStatusLabel(b.status)}</span></div>
      <div class="meta">${esc(b.created_at)} · ${b.episodes_per_topic} tập/chủ đề · ${b.minutes} phút/tập</div>
      <div class="progress"><div style="width:${p.pct}%"></div></div>
      <div class="meta">${p.done}/${p.total} kịch bản xong${p.failed ? ` · ${p.failed} lỗi` : ""}</div>
      <div class="row">
        ${b.status === "running" ? `<button class="btn small batch-ctrl" data-id="${esc(b.id)}" data-action="pause">⏸ Tạm dừng</button><button class="btn small batch-ctrl" data-id="${esc(b.id)}" data-action="cancel">⏹ Dừng hẳn</button>` : ""}
        ${b.status === "paused" ? `<button class="btn small batch-ctrl" data-id="${esc(b.id)}" data-action="resume">▶ Tiếp tục</button><button class="btn small batch-ctrl" data-id="${esc(b.id)}" data-action="cancel">⏹ Dừng hẳn</button>` : ""}
        ${["interrupted", "failed", "partial", "cancelled"].includes(b.status) ? `<button class="btn small batch-ctrl" data-id="${esc(b.id)}" data-action="restart">↻ Chạy tiếp phần còn lại</button>` : ""}
      </div>
      ${b.status === "paused" ? `<p class="hint">Đợt đang tạm dừng: kịch bản chưa bắt đầu sẽ chờ, kịch bản đang viết viết nốt rồi dừng. Bấm “Tiếp tục” để chạy tiếp.</p>` : ""}
      ${b.status === "cancelled" ? `<p class="hint">Đợt đã dừng hẳn: không bắt đầu kịch bản mới nữa. Kịch bản đã xong vẫn giữ nguyên, bấm “Chạy tiếp” nếu muốn làm nốt.</p>` : ""}
      ${b.status === "interrupted" ? `<p class="hint">Đợt bị gián đoạn (server restart/crash): bấm “Chạy tiếp” để làm nốt phần chưa xong, kịch bản đã xong không viết lại.</p>` : ""}
      ${topicsHtml}
    </div>`;
  }).join("");
}
async function loadBatches() {
  try {
    renderBatches(await api("/api/batches"));
  } catch (e) {
    $("#batches-list").innerHTML = `Không tải được đợt: ${esc(e.message)}`;
  }
}
function startBatchPolling() {
  if (batchPollTimer) return;
  batchPollTimer = setInterval(async () => {
    try {
      const batches = await api("/api/batches");
      renderBatches(batches);
      if (!batches.some((b) => b.status === "running" || b.status === "paused")) {
        clearInterval(batchPollTimer); batchPollTimer = null;
        loadScripts();
      }
    } catch (e) { /* đợt sau sẽ thử lại */ }
  }, 10000);
}
// Nút điều khiển đợt (tạm dừng / tiếp tục / dừng hẳn / chạy tiếp).
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".batch-ctrl");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "cancel" && !confirm("Dừng hẳn đợt này? Kịch bản đang viết sẽ viết nốt rồi dừng, kịch bản chưa bắt đầu sẽ không chạy nữa.")) return;
  btn.disabled = true;
  try {
    await api(`/api/batches/${encodeURIComponent(btn.dataset.id)}/${action}`, { method: "POST" });
    loadBatches();
    if (action === "restart") startBatchPolling();
  } catch (err) {
    alert(err.message);
    btn.disabled = false;
  }
});
$("#btn-refresh-batches").addEventListener("click", loadBatches);
$("#btn-batch-trending").addEventListener("click", async () => {
  const msg = $("#batch-msg");
  const count = parseInt($("#batch-topic-count").value, 10) || 5;
  msg.className = "msg"; msg.textContent = "Muse đang phân tích xu hướng tìm kiếm… (có thể mất 1–2 phút)";
  try {
    const r = await api("/api/trending-topics", { method: "POST", body: JSON.stringify({ count }) });
    $("#batch-topics").value = r.topics.map((t) => t.title).join("\n");
    $("#batch-topics-preview").innerHTML = (r.trend_based
      ? "<p class='hint'>Chủ đề theo phân tích xu hướng — lý do từng chủ đề (bạn sửa lại trong ô trước khi tạo):</p>"
      : "<p class='hint'>⚠️ Không phân tích được xu hướng (không có acc hoặc gateway lỗi) — đây là list mẫu, bạn sửa lại trước khi tạo.</p>")
      + r.topics.map((t) => `<p><b>${esc(t.title)}</b> — ${esc(t.reason)}</p>`).join("");
    msg.className = "msg ok"; msg.textContent = `Đã gợi ý ${r.topics.length} chủ đề.`;
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});
$("#btn-batch-create").addEventListener("click", async () => {
  const msg = $("#batch-msg");
  const topics = $("#batch-topics").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const body = {
    topics,
    topic_count: parseInt($("#batch-topic-count").value, 10) || 2,
    episodes_per_topic: parseInt($("#batch-episodes").value, 10) || 10,
    minutes: parseInt($("#batch-minutes").value, 10) || 8,
  };
  msg.className = "msg"; msg.textContent = "Đang tạo đợt…";
  try {
    const r = await api("/api/batch-create", { method: "POST", body: JSON.stringify(body) });
    msg.className = "msg ok";
    const src = r.topics_source === "trend" ? " (chủ đề theo xu hướng)"
      : r.topics_source === "fallback" ? " (chủ đề từ list mẫu — không phân tích được xu hướng)" : "";
    msg.textContent = `Đã tạo đợt ${r.batch_id}: ${r.total_scripts} kịch bản (${r.topics.length} chủ đề × ${r.episodes_per_topic} tập, ${r.minutes} phút/tập)${src}. Đợt chạy nền — xem tiến độ bên dưới; mỗi kịch bản xong sẽ báo qua Telegram (nếu đã cấu hình).`;
    loadBatches();
    startBatchPolling();
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});
$("#btn-suggest-topics").addEventListener("click", async () => {
  const msg = $("#create-msg");
  const box = $("#topics-list");
  msg.className = "msg"; msg.textContent = "Đang gợi ý chủ đề…";
  box.innerHTML = "";
  try {
    const r = await api("/api/topics", { method: "POST", body: JSON.stringify({
      keyword: $("#create-keyword").value.trim(),
      count: 10,
      account: $("#create-account").value,
    }) });
    if (!r.items.length) { msg.textContent = "Không gợi ý được chủ đề nào."; return; }
    msg.textContent = `Gợi ý ${r.items.length} chủ đề — bấm vào một chủ đề để dùng.`;
    box.innerHTML = `<ul class="list">` + r.items.map((t) => `
      <li data-title="${esc(t.title)}"><span><b>${esc(t.title)}</b>
      <span class="meta">${esc(t.summary || "")}</span></span></li>`).join("") + `</ul>`;
    $$("#topics-list li").forEach((li) => li.addEventListener("click", () => {
      $("#create-topic").value = li.dataset.title;
      msg.textContent = `Đã chọn chủ đề: ${li.dataset.title} — bấm “Viết kịch bản”.`;
    }));
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});
$("#btn-write-script").addEventListener("click", async () => {
  const msg = $("#create-msg");
  const topic = $("#create-topic").value.trim();
  if (!topic) { msg.className = "msg err"; msg.textContent = "Nhập chủ đề trước đã."; return; }
  msg.className = "msg"; msg.textContent = "Muse đang viết kịch bản… (có thể mất 1–2 phút)";
  try {
    const r = await api("/api/write-script", { method: "POST", body: JSON.stringify({
      topic,
      scenes: parseInt($("#create-scenes").value, 10) || 10,
      duration: parseInt($("#create-duration").value, 10) || 10,
      account: $("#create-account").value,
    }) });
    createdScriptPath = r.path;
    msg.className = "msg ok";
    msg.textContent = `Đã lưu kịch bản: ${r.path} (${r.scenes} cảnh).`;
    $("#create-result").style.display = "";
    $("#create-result-title").textContent = `${r.title} — ${r.path}`;
    const content = await api(`/api/scripts/content?path=${encodeURIComponent(r.path)}`);
    $("#create-result-script").textContent = content.content;
    loadScripts();
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});
$("#btn-edit-created-script").addEventListener("click", () => {
  if (!createdScriptPath) return;
  gotoTab("scripts");
  openScript(createdScriptPath);
});
$("#btn-build-created-script").addEventListener("click", () => {
  if (!createdScriptPath) return;
  gotoTab("build");
  $("#build-script-select").value = createdScriptPath;
});

// ---------------- outputs ----------------
// Phân trang: 1000 video cũng chỉ render 1 trang/lần. Thẻ hiển thị ảnh
// poster (nhẹ) thay cho <video>; bấm vào ảnh mới tải video thật.
let outputsPage = 1, outputsQuery = "", outputsTotal = 0, outputsPages = 1, outputsLoading = false;
function outputCard(o) {
  const m = o.manifest || {};
  const scenes = m.scenes ? m.scenes.length : "?";
  const dur = m.scene_durations
    ? Object.values(m.scene_durations).reduce((a, b) => a + b, 0).toFixed(0) + "s"
    : "";
  const media = o.poster
    ? `<div class="poster" data-final="${esc(o.final || "")}" title="Bấm để xem video"><img src="${esc(o.poster)}" loading="lazy" alt="${esc(m.title || o.project)}"><span class="play">▶</span></div>`
    : (o.final ? `<video controls preload="none" src="${esc(o.final)}"></video>` : "");
  return `<div class="output-card">
    ${media}
    <div class="body">
      <h3>${esc(m.title || o.project)}</h3>
      <div class="meta">${esc(o.project)} · ${scenes} cảnh · ${dur} · ${(o.size / 1048576).toFixed(1)} MB
        ${m.mock ? "· mock" : ""}
        ${o.script_name ? `· kịch bản: ${esc(o.script_name)}` : ""}</div>
      <div class="meta">📁 ${esc(o.output_path || "")}</div>
      <div class="actions">
        <label><input type="checkbox" class="output-check" data-project="${esc(o.project)}"> Chọn</label>
        ${o.final ? `<a href="${esc(o.final)}" download>⬇ Tải video</a>` : ""}
        ${o.narration_txt ? `<a href="${esc(o.narration_txt)}" download>⬇ Lời dẫn .txt</a>` : ""}
        ${o.subtitles ? `<a href="${esc(o.subtitles)}" download>⬇ Phụ đề .srt</a>` : ""}
        <button class="btn small" data-del-output="${esc(o.project)}">🗑 Xóa video</button>
      </div>
    </div>
  </div>`;
}
document.addEventListener("click", (e) => {
  const del = e.target.closest("[data-del-output]");
  if (del) {
    e.preventDefault();
    outputsSelected.delete(del.dataset.delOutput);
    if (!confirm(`Xóa video "${del.dataset.delOutput}"? (xóa cả thư mục output, không khôi phục được)`)) return;
    api(`/api/outputs/${encodeURIComponent(del.dataset.delOutput)}`, { method: "DELETE" })
      .then(() => loadOutputs())
      .catch((err) => alert(err.message));
    return;
  }
});
$("#btn-select-all-outputs").addEventListener("click", () => {
  const rows = outputsRows.filter((o) => outputsFilter === "all" || outputStatusKey(o) === outputsFilter);
  const allChecked = rows.length && rows.every((o) => outputsSelected.has(o.project));
  for (const o of rows) {
    if (allChecked) outputsSelected.delete(o.project); else outputsSelected.add(o.project);
  }
  renderOutputsTable();
});
$("#btn-download-zip").addEventListener("click", async () => {
  const msg = $("#outputs-msg");
  const projects = [...outputsSelected];
  if (!projects.length) {
    msg.className = "msg err"; msg.textContent = "Chưa tích chọn video nào.";
    return;
  }
  msg.className = "msg"; msg.textContent = `Đang nén ${projects.length} video…`;
  try {
    const r = await api("/api/results/download-zip", { method: "POST", body: JSON.stringify({ projects }) });
    const a = document.createElement("a");
    a.href = r.download_url; a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
    msg.className = "msg ok";
    msg.textContent = `Đã nén ${r.projects} video (${r.files} file, ${(r.size / 1048576).toFixed(1)} MB) — trình duyệt đang tải xuống.`;
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});
document.addEventListener("click", (e) => {
  const poster = e.target.closest(".poster");
  if (!poster || !poster.dataset.final) return;
  const video = document.createElement("video");
  video.controls = true; video.autoplay = true; video.preload = "metadata";
  video.src = poster.dataset.final;
  poster.replaceWith(video);
});
// ---------------- outputs: bảng (lọc trạng thái, tích chọn) ----------------
// outputCard giữ nguyên cho khối "video mới nhất" ở tab Tổng quan.
let outputsRows = []; // tích lũy qua các trang đã tải
let outputsFilter = "all"; // all | done | waiting
let outputsSelected = new Set();
function outputStatusKey(o) { return o.final ? "done" : "waiting"; }
function outputTableRow(o, stt) {
  const m = o.manifest || {};
  const scenes = m.scenes ? m.scenes.length : "?";
  const dur = m.scene_durations
    ? Object.values(m.scene_durations).reduce((a, b) => a + b, 0).toFixed(0) + "s"
    : "";
  const thumb = o.poster
    ? `<a href="${esc(o.final || "")}" target="_blank" rel="noopener"><img class="thumb" src="${esc(o.poster)}" loading="lazy" alt="${esc(m.title || o.project)}"></a>`
    : (o.final ? `<a href="${esc(o.final)}" target="_blank" rel="noopener"><div class="thumb-ph">▶</div></a>` : `<div class="thumb-ph">—</div>`);
  const accounts = (o.accounts && o.accounts.length) ? o.accounts.join(", ") : "—";
  const checked = outputsSelected.has(o.project) ? "checked" : "";
  return `<tr>
    <td class="chk"><input type="checkbox" class="output-check" data-check="${esc(o.project)}" ${checked}></td>
    <td class="stt">${stt}</td>
    <td><div class="name-cell">${thumb}<div class="name-text"><div class="row-title">${esc(m.title || o.project)}</div><div class="row-sub">${esc(o.project)} · ${scenes} cảnh · ${dur} · ${(o.size / 1048576).toFixed(1)} MB${o.script_name ? ` · kịch bản: ${esc(o.script_name)}` : ""}</div></div></div></td>
    <td>Video</td>
    <td>${esc(accounts)}</td>
    <td>${o.final ? `<span class="badge ok">Xong</span>` : `<span class="badge">Chờ</span>`}</td>
    <td><div class="row-actions">
      ${o.final ? `<a class="btn small" href="${esc(o.final)}" target="_blank" rel="noopener">▶ Xem</a> <a class="btn small" href="${esc(o.final)}" download>⬇ Tải</a>` : ""}
      <button class="btn small danger" data-del-output="${esc(o.project)}">🗑 Xóa</button>
    </div></td>
  </tr>`;
}
function renderOutputsTable() {
  const rows = outputsRows.filter((o) => outputsFilter === "all" || outputStatusKey(o) === outputsFilter);
  const counts = { all: outputsRows.length, done: 0, waiting: 0 };
  for (const o of outputsRows) counts[outputStatusKey(o)]++;
  $("#outputs-pills").innerHTML = pillsHtml(counts, outputsFilter, [["all", "Tất cả"], ["done", "Xong"], ["waiting", "Chờ"]]);
  $("#outputs-tbody").innerHTML = rows.map((o, i) => outputTableRow(o, i + 1)).join("")
    || `<tr><td colspan="7" style="text-align:center;color:var(--muted)">Chưa có video nào.</td></tr>`;
  const visible = rows.map((o) => o.project);
  $("#outputs-check-all").checked = visible.length > 0 && visible.every((p) => outputsSelected.has(p));
}
async function loadOutputs(reset = true) {
  if (outputsLoading) return;
  if (reset) {
    outputsPage = 1;
    outputsQuery = $("#outputs-search").value.trim();
    outputsRows = [];
  }
  outputsLoading = true;
  try {
    const data = await api(`/api/outputs?page=${outputsPage}&per_page=20&q=${encodeURIComponent(outputsQuery)}`);
    outputsTotal = data.total; outputsPages = data.pages;
    const list = data.items;
    $("#outputs-count").textContent = outputsTotal ? `(${outputsTotal})` : "";
    $("#outputs-count-text").textContent = `Đã tải ${outputsRows.length + list.length} / ${outputsTotal} video`;
    outputsRows.push(...list);
    renderOutputsTable();
    $("#btn-more-outputs").style.display = outputsPage < outputsPages ? "" : "none";
    if (outputsPage === 1) $("#latest-outputs").innerHTML = list.slice(0, 3).map(outputCard).join("") || "Chưa có video nào — tạo video đầu tiên ở tab <b>Tạo video</b>.";
  } catch (e) {
    if (outputsPage === 1) $("#outputs-tbody").innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted)">Không tải được kết quả: ${esc(e.message)}</td></tr>`;
  } finally { outputsLoading = false; }
}
$("#outputs-pills").addEventListener("click", (e) => {
  const pill = e.target.closest("[data-pill]");
  if (!pill) return;
  outputsFilter = pill.dataset.pill;
  renderOutputsTable();
});
$("#outputs-tbody").addEventListener("click", (e) => {
  const check = e.target.closest(".output-check");
  if (check) {
    const p = check.dataset.check;
    if (check.checked) outputsSelected.add(p); else outputsSelected.delete(p);
    renderOutputsTable();
  }
});
$("#outputs-check-all").addEventListener("change", (e) => {
  const on = e.target.checked;
  const rows = outputsRows.filter((o) => outputsFilter === "all" || outputStatusKey(o) === outputsFilter);
  for (const o of rows) {
    if (on) outputsSelected.add(o.project); else outputsSelected.delete(o.project);
  }
  renderOutputsTable();
});
$("#btn-del-selected-outputs").addEventListener("click", async () => {
  const projects = [...outputsSelected];
  if (!projects.length) return alert("Chưa tích chọn video nào.");
  if (!confirm(`Xóa ${projects.length} video? (xóa cả thư mục output, không khôi phục được)`)) return;
  const msg = $("#outputs-msg");
  let ok = 0;
  for (const p of projects) {
    try { await api(`/api/outputs/${encodeURIComponent(p)}`, { method: "DELETE" }); ok++; }
    catch (e) { /* báo tổng cuối */ }
  }
  outputsSelected.clear();
  msg.className = "msg ok"; msg.textContent = `Đã xóa ${ok}/${projects.length} video.`;
  loadOutputs();
});
let outputsSearchTimer = null;
$("#outputs-search").addEventListener("input", () => {
  clearTimeout(outputsSearchTimer);
  outputsSearchTimer = setTimeout(() => loadOutputs(true), 300);
});
$("#btn-more-outputs").addEventListener("click", () => { outputsPage++; loadOutputs(false); });

// ---------------- init ----------------
loadStatus(); loadAccounts(); loadScripts(); loadOutputs(); loadJobs();
setInterval(loadStatus, 30000);
setInterval(loadJobs, 10000); // bảng job tự cập nhật (hàng đợi chạy tuần tự)

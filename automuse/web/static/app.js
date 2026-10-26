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
    const be = $("#badge-elevenlabs");
    be.textContent = s.elevenlabs_configured ? "ElevenLabs ✓" : "ElevenLabs chưa có key";
    be.className = "badge " + (s.elevenlabs_configured ? "ok" : "warn");
    $("#stat-cards").innerHTML = `
      <div class="stat"><div class="num">${s.accounts}</div><div class="label">Tài khoản Muse</div></div>
      <div class="stat"><div class="num">${s.outputs}</div><div class="label">Video đã dựng</div></div>
      <div class="stat"><div class="num">${s.elevenlabs_configured ? "Có" : "Chưa"}</div><div class="label">ElevenLabs key</div></div>
      <div class="stat"><div class="num">${s.ffmpeg ? "OK" : "Thiếu"}</div><div class="label">ffmpeg</div></div>`;
    const steps = [];
    if (!s.accounts_ready) steps.push("Thêm tài khoản Muse ở tab <b>Tài khoản Muse</b> (cookie + proxy).");
    if (!s.elevenlabs_configured) steps.push("Bỏ <code>ELEVENLABS_API_KEY</code> vào file <code>.env</code> rồi khởi động lại server để lồng tiếng thật.");
    steps.push("Chọn một kịch bản ở tab <b>Kịch bản</b> rồi bấm <b>Tạo video</b> — thử bằng mock trước cũng được.");
    if (s.outputs) steps.push(`Bạn đã có ${s.outputs} video trong tab <b>Kết quả</b>.`);
    $("#next-steps").innerHTML = steps.map((x) => `<li>${x}</li>`).join("");
  } catch (e) {
    $("#stat-cards").innerHTML = `<div class="stat">Không kết nối được server: ${esc(e.message)}</div>`;
  }
}

// ---------------- accounts ----------------
async function loadAccounts() {
  const list = await api("/api/accounts");
  const tbody = $("#accounts-table tbody");
  tbody.innerHTML = list.map((a) => `
    <tr>
      <td><b>${esc(a.name)}</b><br><span class="meta">${esc(a.note || "")}</span></td>
      <td>${esc(a.proxy || "-")}</td>
      <td>${a.jobs_done} ✓ / ${a.jobs_failed} ✗</td>
      <td>${!a.enabled ? "tắt" : '<span class="badge ok">sẵn sàng</span>'}
          <span class="check-result" id="check-${esc(a.name)}"></span></td>
      <td>
        <button class="btn small" onclick="checkAccount('${esc(a.name)}')">Kiểm tra</button>
        <button class="btn small danger" onclick="deleteAccount('${esc(a.name)}')">Xóa</button>
      </td>
    </tr>`).join("") || `<tr><td colspan="5">Chưa có tài khoản nào.</td></tr>`;
  const sel = $("#build-accounts-select");
  sel.innerHTML = list.filter((a) => a.enabled)
    .map((a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("");
}
async function checkAccount(name) {
  const el = $("#check-" + name);
  el.textContent = " đang kiểm tra…";
  try {
    const r = await api(`/api/accounts/${encodeURIComponent(name)}/check`, { method: "POST" });
    const parts = [];
    if (r.proxy) parts.push(`proxy ${r.proxy.ok ? "✓" : "✗"} (${esc(r.proxy.message)})`);
    parts.push(`login ${r.login.ok ? "✓" : "✗"} (${esc(r.login.message)})`);
    el.innerHTML = parts.join("<br>");
  } catch (e) {
    el.textContent = " lỗi: " + e.message;
  }
}
async function deleteAccount(name) {
  if (!confirm(`Xóa tài khoản ${name}?`)) return;
  await api(`/api/accounts/${encodeURIComponent(name)}`, { method: "DELETE" });
  loadAccounts(); loadStatus();
}
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

// ---------------- scripts ----------------
let currentScriptPath = null;
async function loadScripts() {
  const list = await api("/api/scripts");
  $("#scripts-list").innerHTML = list.map((s) => `
    <li data-path="${esc(s.path)}" class="${s.path === currentScriptPath ? "selected" : ""}">
      <span><b>${esc(s.name)}</b> <span class="meta">${esc(s.source)} · ${esc(s.kind)} · ${s.scenes} cảnh</span></span>
      <span class="meta">${esc(s.file)}</span>
    </li>`).join("");
  $$("#scripts-list li").forEach((li) => li.addEventListener("click", () => openScript(li.dataset.path)));
  const sel = $("#build-script-select");
  const prev = sel.value;
  sel.innerHTML = list.map((s) => `<option value="${esc(s.path)}">${esc(s.name)} (${esc(s.source)} · ${s.scenes} cảnh)</option>`).join("");
  if (prev) sel.value = prev;
}
async function openScript(path) {
  const data = await api(`/api/scripts/content?path=${encodeURIComponent(path)}`);
  currentScriptPath = path;
  $("#editor-title").textContent = path;
  $("#script-editor").value = data.content;
  loadScripts();
}
$("#btn-new-script").addEventListener("click", () => {
  const name = prompt("Tên kịch bản mới (vd: phim-ma):");
  if (!name) return;
  currentScriptPath = `scripts/${name}.yaml`;
  $("#editor-title").textContent = currentScriptPath + " (mới)";
  $("#script-editor").value = `title: "${name}"
kind: movie
aspect: "16:9"
default_voice: narrator
voices:
  narrator: ""
scenes:
  - id: s01
    title: "Cảnh 1"
    duration: 6
    visual: "Describe the scene in English for Muse video generation..."
    narration: "Lời thoại tiếng Việt ở đây."
    voice: narrator
`;
});
async function loadTemplate(kind) {
  const path = `templates/${kind}_script.yaml`;
  await openScript(path);
  currentScriptPath = null; // template chỉ để xem, lưu sẽ hỏi tên mới
  $("#editor-title").textContent = path + " (mẫu — bấm Lưu để tạo bản sao)";
}
$("#btn-template-movie").addEventListener("click", () => loadTemplate("movie"));
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
    currentScriptPath = r.path; loadScripts();
  } catch (e) {
    msg.className = "msg err"; msg.textContent = e.message;
  }
});
$("#btn-build-script").addEventListener("click", () => {
  if (!currentScriptPath) return alert("Chọn kịch bản trước đã.");
  gotoTab("build");
  $("#build-script-select").value = currentScriptPath;
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
    no_tts: fd.get("no_tts") === "on",
    no_iris: fd.get("no_iris") === "on",
    resume: fd.get("resume") === "on",
    out: fd.get("out") || "",
    music: fd.get("music") || "",
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
      if (job.status === "done" || job.status === "failed") {
        clearInterval(pollTimer);
        badge.className = "badge " + (job.status === "done" ? "ok" : "bad");
        badge.textContent = job.status === "done" ? "xong ✓" : "lỗi ✗";
        if (job.status === "done") bar.style.width = "100%";
        if (job.error) logEl.textContent += "\nLỖI: " + job.error;
        loadJobs(); loadOutputs(); loadStatus();
      }
    } catch (e) { /* server mất kết nối tạm thời thì poll tiếp */ }
  }, 2000);
}
async function loadJobs() {
  const jobs = await api("/api/jobs");
  $("#jobs-list").innerHTML = jobs.map((j) => `
    <li>
      <span><b>${esc(j.id)}</b> · ${esc(j.script)} ${j.mock ? "(mock)" : ""}
        <span class="meta">${j.created_at} → ${j.status}</span></span>
      <button class="btn small" onclick="watchJob('${esc(j.id)}')">Xem log</button>
    </li>`).join("") || "<li>Chưa có job nào.</li>";
}

// ---------------- outputs ----------------
function outputCard(o) {
  const m = o.manifest || {};
  const scenes = m.scenes ? m.scenes.length : "?";
  const dur = m.scene_durations
    ? Object.values(m.scene_durations).reduce((a, b) => a + b, 0).toFixed(0) + "s"
    : "";
  return `<div class="output-card">
    ${o.final ? `<video controls preload="metadata" src="${esc(o.final)}"></video>` : ""}
    <div class="body">
      <h3>${esc(m.title || o.project)}</h3>
      <div class="meta">${esc(o.project)} · ${scenes} cảnh · ${dur} · ${(o.size / 1048576).toFixed(1)} MB
        ${m.mock ? "· mock" : ""}</div>
      <div class="actions">
        ${o.final ? `<a href="${esc(o.final)}" download>⬇ Tải video</a>` : ""}
        ${o.subtitles ? `<a href="${esc(o.subtitles)}" download>⬇ Phụ đề .srt</a>` : ""}
      </div>
    </div>
  </div>`;
}
async function loadOutputs() {
  const list = await api("/api/outputs");
  $("#outputs-count").textContent = list.length ? `(${list.length})` : "";
  $("#outputs-grid").innerHTML = list.map(outputCard).join("") || "Chưa có video nào.";
  $("#latest-outputs").innerHTML = list.slice(0, 3).map(outputCard).join("") || "Chưa có video nào — tạo video đầu tiên ở tab <b>Tạo video</b>.";
}

// ---------------- init ----------------
loadStatus(); loadAccounts(); loadScripts(); loadOutputs(); loadJobs();
setInterval(loadStatus, 30000);

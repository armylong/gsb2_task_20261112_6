/* 本地音频工作台前端: 原生 JS, 无构建步骤、无外部依赖。 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const api = async (path, options = {}) => {
    const res = await fetch(path, {
      headers: options.body instanceof FormData
        ? undefined
        : { "Content-Type": "application/json" },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || `HTTP ${res.status}`),
      { status: res.status, data });
    return data;
  };

  const state = {
    sources: [],
    jobs: [],
    selectedSourceId: null,
    meta: { formats: [], sample_rates: [], max_concurrency: 8 },
    pollTimer: null,
  };

  // ---------------- 工具 ----------------
  const escapeHtml = (s) => String(s ?? "").replace(
    /[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const fmtDuration = (sec) => {
    if (sec == null) return "—";
    const m = Math.floor(sec / 60);
    const s = (sec % 60).toFixed(2).padStart(5, "0");
    return `${m}:${s}`;
  };
  const fmtSize = (bytes) => {
    if (bytes == null) return "—";
    const units = ["B", "KB", "MB", "GB"];
    let v = bytes, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  };
  const channelLabel = (ch) =>
    ch == null ? "—" : ch === 1 ? "单声道" : ch === 2 ? "立体声" : `${ch} 声道`;
  const peakLabel = (db) => db == null ? "—" : `${db.toFixed(1)} dBFS`;
  const srLabel = (sr) => sr ? `${(sr / 1000).toFixed(1)} kHz` : "—";

  function showBanner(el, text, kind = "info") {
    el.textContent = text;
    el.className = `banner ${kind}`;
    el.hidden = false;
  }
  function hideBanner(el) { el.hidden = true; }

  const STATUS_BADGE = {
    queued: ["排队中", "badge-queued"],
    running: ["运行中", "badge-running"],
    completed: ["完成", "badge-completed"],
    failed: ["失败", "badge-failed"],
    canceled: ["已取消", "badge-canceled"],
  };

  // ---------------- 初始化 ----------------
  async function init() {
    state.meta = await api("/api/meta");
    $("#max-upload-label").textContent = state.meta.max_upload_mb;
    $("#concurrency-input").max = state.meta.max_concurrency;
    $("#concurrency-input").value = state.meta.concurrency;
    fillFormOptions();
    bindEvents();
    await refreshAll();
    // 2 秒轮询: 状态与进度在刷新页面后同样持续可见(数据全部来自服务端)
    state.pollTimer = setInterval(refreshJobsOnly, 2000);
  }

  function fillFormOptions() {
    const fmt = $("#opt-format");
    fmt.innerHTML = state.meta.formats
      .map((f) => `<option value="${f}">${f.toUpperCase()}</option>`).join("");
    const sr = $("#opt-sample-rate");
    sr.innerHTML = state.meta.sample_rates
      .map((v) => `<option value="${v}">${(v / 1000).toFixed(1)} kHz</option>`)
      .join("");
    sr.value = "48000";
  }

  function bindEvents() {
    const dz = $("#dropzone"), fi = $("#file-input");
    dz.addEventListener("click", () => fi.click());
    dz.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") fi.click();
    });
    fi.addEventListener("change", () => uploadFiles(fi.files));
    ["dragenter", "dragover"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragover"); }));
    dz.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));

    $("#import-local-btn").addEventListener("click", importLocalPath);
    $("#save-concurrency").addEventListener("click", saveConcurrency);
    $("#submit-job").addEventListener("click", submitJob);
    $("#cancel-form").addEventListener("click", closeJobForm);
    $("#close-compare").addEventListener("click", closeCompare);
    $("#compare-modal").addEventListener("click", (e) => {
      if (e.target.id === "compare-modal") closeCompare();
    });
  }

  // ---------------- 导入 ----------------
  async function uploadFiles(fileList) {
    const banner = $("#import-message");
    const files = [...fileList];
    if (!files.length) return;
    let okCount = 0;
    const errors = [];
    for (const file of files) {
      const fd = new FormData();
      fd.append("file", file);
      try {
        await api("/api/sources", { method: "POST", body: fd });
        okCount++;
      } catch (err) {
        errors.push(`${file.name}: ${err.message}`);
      }
    }
    if (okCount) showBanner(banner, `成功导入 ${okCount} 个文件${
      errors.length ? `; 失败 ${errors.length} 个` : ""}`, errors.length ? "info" : "success");
    else showBanner(banner, errors.join(" | "), "error");
    await refreshAll();
  }

  async function importLocalPath() {
    const input = $("#local-path");
    const path = input.value.trim();
    const banner = $("#import-message");
    if (!path) return showBanner(banner, "请填写本机文件路径", "error");
    try {
      await api("/api/sources/import", {
        method: "POST", body: JSON.stringify({ path }),
      });
      showBanner(banner, "导入成功(文件已复制进工作区)", "success");
      input.value = "";
      await refreshAll();
    } catch (err) {
      showBanner(banner, err.message, "error");
    }
  }

  // ---------------- 渲染: 源文件 ----------------
  function renderSources() {
    const tbody = $("#sources-tbody");
    if (!state.sources.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">还没有导入文件</td></tr>';
      return;
    }
    tbody.innerHTML = state.sources.map((s) => `
      <tr>
        <td class="mono">${s.id}</td>
        <td title="${escapeHtml(s.filename)}">${escapeHtml(s.filename)}</td>
        <td>${fmtDuration(s.duration)}</td>
        <td>${srLabel(s.sample_rate)}</td>
        <td>${channelLabel(s.channels)}</td>
        <td>${peakLabel(s.peak_db)}</td>
        <td>${fmtSize(s.size_bytes)}</td>
        <td>
          <div class="row-actions">
            <button class="btn btn-small" onclick="wb.previewSource(${s.id})">试听</button>
            <button class="btn btn-small btn-primary" onclick="wb.openJobForm(${s.id})">建任务</button>
          </div>
        </td>
      </tr>`).join("");
  }

  // ---------------- 渲染: 任务 ----------------
  function optionsText(o) {
    const parts = [
      (o.format || "").toUpperCase(),
      o.sample_rate ? srLabel(o.sample_rate) : "",
      o.loudness_lufs ? `响度 ${o.loudness_lufs} LUFS` : "响度不调整",
      o.fade_seconds > 0 ? `淡变 ${o.fade_seconds}s` : "无淡变",
    ];
    return parts.filter(Boolean).join(" · ");
  }

  function renderJobs() {
    const tbody = $("#jobs-tbody");
    if (!state.jobs.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">还没有任务</td></tr>';
      $("#queue-summary").textContent = "";
      return;
    }
    const counts = state.jobs.reduce((acc, j) => {
      acc[j.status] = (acc[j.status] || 0) + 1; return acc;
    }, {});
    $("#queue-summary").textContent =
      `(排队 ${counts.queued || 0} · 运行 ${counts.running || 0} · 完成 ${counts.completed || 0
      } · 失败 ${counts.failed || 0} · 取消 ${counts.canceled || 0})`;

    tbody.innerHTML = state.jobs.map((j) => {
      const [badgeText, badgeClass] = STATUS_BADGE[j.status] || [j.status, ""];
      const active = j.status === "queued" || j.status === "running";
      const pct = Math.round(j.progress || 0);
      let resultCell = "—";
      if (j.status === "completed") {
        resultCell = `<div class="output-name" title="${escapeHtml(j.output_name)}">${escapeHtml(j.output_name)}</div>
          <div class="hint">${fmtSize(j.output_size)}</div>`;
      } else if (j.status === "failed" || j.status === "canceled") {
        resultCell = `<div class="error-text" title="${escapeHtml(j.error || "")}">${escapeHtml(j.error || "")}</div>`;
      }
      const actions = [
        `<button class="btn btn-small" onclick="wb.previewSource(${j.source_id})">听源文件</button>`,
      ];
      if (j.status === "completed") {
        actions.push(`<button class="btn btn-small" onclick="wb.openCompare(${j.id})">前后对比</button>`);
        actions.push(`<a class="btn btn-small btn-primary" href="/api/jobs/${j.id}/download" download>下载</a>`);
      }
      if (active) {
        actions.push(`<button class="btn btn-small btn-danger" onclick="wb.cancelJob(${j.id})">取消</button>`);
      }
      return `
      <tr>
        <td class="mono">${j.id}</td>
        <td title="${escapeHtml(j.source_filename)}">${escapeHtml(j.source_filename)}</td>
        <td>${escapeHtml(optionsText(j.options))}</td>
        <td><span class="badge ${badgeClass}">${badgeText}</span></td>
        <td>
          <div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div>
          <div class="progress-label">${active ? pct + "%" : "—"}</div>
        </td>
        <td>${resultCell}</td>
        <td><div class="row-actions">${actions.join("")}</div></td>
      </tr>`;
    }).join("");
  }

  // ---------------- 任务表单 ----------------
  window.wb = {
    openJobForm(sourceId) {
      const s = state.sources.find((x) => x.id === sourceId);
      if (!s) return;
      state.selectedSourceId = sourceId;
      $("#job-form-source").textContent = s.filename;
      $("#job-form-card").hidden = false;
      $("#job-form-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
      hideBanner($("#job-form-message"));
    },
    async previewSource(sourceId) {
      const s = state.sources.find((x) => x.id === sourceId);
      if (!s) return;
      // 复用对比弹窗的源音频区做快速试听
      $("#compare-job-id").textContent = `源文件 #${sourceId}`;
      $("#audio-source").src = `/api/sources/${sourceId}/stream?t=${Date.now()}`;
      $("#audio-output").removeAttribute("src");
      fillMetaDl($("#compare-source-meta"), {
        文件名: s.filename, 时长: fmtDuration(s.duration),
        采样率: srLabel(s.sample_rate), 声道: channelLabel(s.channels),
        峰值: peakLabel(s.peak_db), 编码: s.codec_name || "—",
        大小: fmtSize(s.size_bytes),
      });
      fillMetaDl($("#compare-output-meta"), { "": "此视图仅试听源文件" });
      $("#compare-modal").hidden = false;
      $("#audio-source").play().catch(() => {});
    },
    async cancelJob(jobId) {
      if (!confirm("确认取消该任务? ffmpeg 进程将被终止, 临时文件会清理。")) return;
      try {
        await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
        await refreshJobsOnly();
      } catch (err) {
        alert(`取消失败: ${err.message}`);
      }
    },
    async openCompare(jobId) {
      const job = state.jobs.find((x) => x.id === jobId);
      if (!job) return;
      $("#compare-job-id").textContent = `#${jobId}`;
      const stamp = `?t=${Date.now()}`;
      $("#audio-source").src = `/api/sources/${job.source_id}/stream${stamp}`;
      $("#audio-output").src = `/api/jobs/${jobId}/stream${stamp}`;
      fillMetaDl($("#compare-source-meta"), {
        文件名: job.source_filename,
        时长: fmtDuration(job.source_duration),
        采样率: srLabel(job.source_sample_rate),
        声道: channelLabel(job.source_channels),
        峰值: peakLabel(job.source_peak_db),
      });
      fillMetaDl($("#compare-output-meta"), { "": "正在探测输出文件…" });
      $("#compare-modal").hidden = false;
      try {
        const info = await api(`/api/jobs/${jobId}/output-info`);
        fillMetaDl($("#compare-output-meta"), {
          文件名: job.output_name,
          时长: fmtDuration(info.duration),
          采样率: srLabel(info.sample_rate),
          声道: channelLabel(info.channels),
          峰值: peakLabel(info.peak_db),
          均方响度: info.rms_db == null ? "—" : `${info.rms_db.toFixed(1)} dBFS`,
          编码: info.codec_name || "—",
          大小: fmtSize(job.output_size),
        });
      } catch (err) {
        fillMetaDl($("#compare-output-meta"), { "探测失败": err.message });
      }
    },
  };

  function fillMetaDl(dl, obj) {
    dl.innerHTML = Object.entries(obj).map(([k, v]) =>
      `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");
  }

  function closeCompare() {
    $("#compare-modal").hidden = true;
    ["#audio-source", "#audio-output"].forEach((sel) => {
      const el = $(sel);
      el.pause();
      el.removeAttribute("src");
      el.load();
    });
  }

  function closeJobForm() {
    $("#job-form-card").hidden = true;
    state.selectedSourceId = null;
  }

  async function submitJob() {
    const banner = $("#job-form-message");
    if (!state.selectedSourceId) return;
    const payload = {
      source_id: state.selectedSourceId,
      format: $("#opt-format").value,
      sample_rate: Number($("#opt-sample-rate").value),
      loudness_lufs: $("#opt-loudness").value === ""
        ? null : Number($("#opt-loudness").value),
      fade_seconds: Number($("#opt-fade").value || 0),
    };
    try {
      await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      showBanner(banner, "任务已入队", "success");
      closeJobForm();
      await refreshJobsOnly();
    } catch (err) {
      // 409 = 同文件进行中任务, 明确提示而不是静默
      showBanner(banner, `${err.status === 409 ? "重复提交被拒绝: " : ""}${err.message}`,
        "error");
    }
  }

  async function saveConcurrency() {
    const value = Number($("#concurrency-input").value);
    try {
      const res = await api("/api/settings/concurrency", {
        method: "POST", body: JSON.stringify({ concurrency: value }),
      });
      $("#concurrency-input").value = res.concurrency;
    } catch (err) {
      alert(err.message);
    }
  }

  // ---------------- 刷新 ----------------
  async function refreshAll() {
    [state.sources, state.jobs] = await Promise.all([
      api("/api/sources"), api("/api/jobs"),
    ]);
    renderSources();
    renderJobs();
  }

  async function refreshJobsOnly() {
    try {
      state.jobs = await api("/api/jobs");
      renderJobs();
    } catch { /* 轮询失败静默, 下轮重试 */ }
  }

  init().catch((err) => {
    document.body.insertAdjacentHTML(
      "afterbegin",
      `<div class="banner error" style="margin:16px">初始化失败: ${escapeHtml(err.message)}</div>`);
  });
})();

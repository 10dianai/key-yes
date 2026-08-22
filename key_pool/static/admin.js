// 管理面板逻辑：登录态管理、批量导入、Key 列表操作。
// 独立于服务代码，改前端只需刷新页面，不用重启服务。

// ---- 面板登录态 ----
// 密码不在页面里，登录成功后拿会话 token（12 小时有效），存 sessionStorage。
const TOKEN_KEY = "keypool_panel_token";
let TOKEN = sessionStorage.getItem(TOKEN_KEY) || "";
const H = () => ({"Content-Type": "application/json", ...(TOKEN ? {"X-Panel-Token": TOKEN} : {})});

function enterPanel() {
  document.getElementById("gate").classList.add("hidden");
  loadStats(); loadKeys();
}

function showGate(mode) {
  // mode: "setup" 首次设置密码 | "login" 登录 | "change" 强制修改密码
  const gate = document.getElementById("gate");
  gate.classList.remove("hidden");
  const title = document.getElementById("gateTitle");
  const desc = document.getElementById("gateDesc");
  const p1 = document.getElementById("gatePassword");
  const p2 = document.getElementById("gatePassword2");
  const p3 = document.getElementById("gatePasswordNew");
  const msg = document.getElementById("gateMsg");
  msg.textContent = "";
  p3.style.display = "none";
  if (mode === "setup") {
    title.textContent = "初始化面板";
    desc.textContent = "首次使用，请设置管理面板密码（至少 6 位）。";
    p1.placeholder = "设置密码（至少 6 位）"; p1.value = "";
    p2.style.display = ""; p2.placeholder = "再输入一次确认"; p2.value = "";
    document.getElementById("gateSubmit").textContent = "保存并进入";
  } else if (mode === "change") {
    title.textContent = "请修改密码";
    desc.textContent = "当前使用的是默认/预设密码，请设置自己的新密码后继续使用。";
    p1.placeholder = "旧密码（已自动填入）";
    p2.style.display = ""; p2.placeholder = "新密码（至少 6 位）"; p2.value = "";
    p3.style.display = ""; p3.placeholder = "再输入一次新密码"; p3.value = "";
    document.getElementById("gateSubmit").textContent = "修改并进入";
  } else {
    title.textContent = "登录管理面板";
    desc.textContent = "输入面板密码继续。";
    p1.placeholder = "面板密码"; p1.value = "";
    p2.style.display = "none"; p2.value = "";
    document.getElementById("gateSubmit").textContent = "进入";
  }
  p1.focus();
}

let changeModeToken = "";  // 强制改密流程中暂存的会话 token

async function gateSubmit() {
  const pwd = document.getElementById("gatePassword").value;
  const pwd2 = document.getElementById("gatePassword2").value;
  const msg = document.getElementById("gateMsg");

  // 强制改密流程：框1=旧密码（自动填入）、框2=新密码、框3=确认新密码
  if (changeModeToken) {
    const oldPwd = document.getElementById("gatePassword").value;
    const newPwd = document.getElementById("gatePassword2").value;
    const confirmPwd = document.getElementById("gatePasswordNew").value;
    if (!newPwd) { msg.textContent = "请输入新密码"; return; }
    if (newPwd !== confirmPwd) { msg.textContent = "两次输入的新密码不一致"; return; }
    const resp = await fetch("/admin/panel/change-password", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Panel-Token": changeModeToken},
      body: JSON.stringify({old_password: oldPwd, new_password: newPwd}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { msg.textContent = data.error || ("请求失败 " + resp.status); return; }
    changeModeToken = "";
    TOKEN = data.token;
    sessionStorage.setItem(TOKEN_KEY, TOKEN);
    document.getElementById("gate").classList.add("hidden");
    document.getElementById("gatePassword").value = "";
    document.getElementById("gatePassword2").value = "";
    document.getElementById("gatePasswordNew").value = "";
    enterPanel();
    return;
  }

  const status = await (await fetch("/admin/panel/status")).json();
  const path = status.initialized ? "/admin/panel/login" : "/admin/panel/setup";
  if (path.endsWith("/setup") && pwd !== pwd2) { msg.textContent = "两次输入不一致"; return; }
  const resp = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({password: pwd}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) { msg.textContent = data.error || ("请求失败 " + resp.status); return; }
  // must_change：登录成功但当前密码来自配置文件（默认/预设），强制先改密
  if (data.must_change) {
    changeModeToken = data.token;
    showGate("change");
    document.getElementById("gatePassword").value = pwd; // 旧密码自动填入
    document.getElementById("gatePassword2").value = "";
    document.getElementById("gatePasswordNew").value = "";
    msg.textContent = "当前是默认密码，请设置你自己的新密码";
    return;
  }
  TOKEN = data.token;
  sessionStorage.setItem(TOKEN_KEY, TOKEN);
  document.getElementById("gatePassword").value = "";
  document.getElementById("gatePassword2").value = "";
  enterPanel();
}

document.getElementById("gateSubmit").onclick = gateSubmit;
document.getElementById("gatePassword").addEventListener("keydown", e => { if (e.key === "Enter") gateSubmit(); });
document.getElementById("gatePassword2").addEventListener("keydown", e => { if (e.key === "Enter") gateSubmit(); });
document.getElementById("gatePasswordNew").addEventListener("keydown", e => { if (e.key === "Enter") gateSubmit(); });

document.getElementById("logout").onclick = async () => {
  await fetch("/admin/panel/logout", {method: "POST", headers: H()});
  sessionStorage.removeItem(TOKEN_KEY);
  TOKEN = "";
  location.reload();
};

async function api(url, opts) {
  const resp = await fetch(url, opts);
  if (resp.status === 401) { showGate("login"); throw new Error("未登录"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || data.detail || resp.status);
  return data;
}

function fmt(n){ return n===null||n===undefined ? "-" : n; }

async function loadStats() {
  const s = await api("/admin/stats", {headers: H()});
  document.getElementById("stats").innerHTML = `
    <div class="card"><div class="lbl">总数</div><div class="num">${s.total}</div></div>
    <div class="card"><div class="lbl">可用</div><div class="num" style="color:var(--ok)">${s.active}</div></div>
    <div class="card"><div class="lbl">已禁用</div><div class="num" style="color:var(--warn)">${s.disabled}</div></div>
    <div class="card"><div class="lbl">已失效</div><div class="num" style="color:var(--bad)">${s.invalid}</div></div>
    <div class="card"><div class="lbl">累计调用</div><div class="num">${s.total_calls}</div></div>`;
}

async function loadKeys() {
  const status = document.getElementById("filter").value;
  const data = await api("/admin/keys?status=" + status, {headers: H()});
  const rows = data.keys.map(k => `
    <tr>
      <td class="mono">${k.id}</td>
      <td class="mono">${k.key_masked}</td>
      <td>${k.label || "-"}</td>
      <td title="${(k.source||"").replace(/"/g,"&quot;")}">${(k.source||"-").split("/").slice(-2).join("/").slice(0,28)}</td>
      <td><span class="tag ${k.status}">${{active:"可用",disabled:"禁用",invalid:"失效"}[k.status]}</span></td>
      <td>${fmt(k.use_count)}</td>
      <td>${fmt(k.fail_count)}</td>
      <td>${fmt(k.last_used)}</td>
      <td>
        ${k.status==="active"
          ? `<button class="small ghost" onclick="toggle('${k.id}','disable')">禁用</button>`
          : `<button class="small ghost" onclick="toggle('${k.id}','enable')">启用</button>`}
        <button class="small ghost" onclick="del('${k.id}')">删除</button>
      </td>
    </tr>`).join("");
  document.getElementById("tbody").innerHTML = rows || '<tr><td colspan="9" style="color:var(--dim)">暂无 Key，先导入一批</td></tr>';
}

function showReport(r) {
  const el = document.getElementById("report");
  el.style.display = "block";
  el.textContent = `导入完成：新增 ${r.added}，重复 ${r.duplicate}，无效 ${r.invalid}`
    + (r.invalid_samples && r.invalid_samples.length ? `\n无效示例: ${r.invalid_samples.join(", ")}` : "")
    + (r.layout ? `\n来源: ${JSON.stringify(r.layout)}` : "");
}

document.getElementById("refresh").onclick = () => { loadStats(); loadKeys(); };
document.getElementById("filter").onchange = loadKeys;

document.getElementById("export").onclick = () => {
  fetch("/admin/export", {headers:H()}).then(r=>{
    if (r.status === 401) { showGate("login"); return ""; }
    return r.text();
  }).then(t=>{
    if (!t) return;
    const blob = new Blob([t], {type:"text/plain"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "pool_keys.txt"; a.click();
  });
};

document.getElementById("clearInvalid").onclick = async () => {
  if (!confirm("确定清空所有“已失效”的 Key？")) return;
  await api("/admin/clear", {method:"POST", headers:H(), body:JSON.stringify({status:"invalid"})});
  loadStats(); loadKeys();
};

async function toggle(id, action) {
  await api(`/admin/keys/${id}/${action}`, {method:"POST", headers:H()});
  loadStats(); loadKeys();
}
async function del(id) {
  if (!confirm(`确定删除 ${id}？`)) return;
  await api(`/admin/keys/${id}`, {method:"DELETE", headers:H()});
  loadStats(); loadKeys();
}

// 上传导入
const drop = document.getElementById("drop");
const fileInput = document.getElementById("file");
drop.onclick = () => fileInput.click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add("hover"); };
drop.ondragleave = () => drop.classList.remove("hover");
drop.ondrop = e => { e.preventDefault(); drop.classList.remove("hover"); upload(e.dataTransfer.files); };
fileInput.onchange = () => upload(fileInput.files);

async function upload(files) {
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    // FormData 上传不能带 Content-Type（需浏览器自动设 multipart 边界），只带 token
    const uploadHeaders = TOKEN ? {"X-Panel-Token": TOKEN} : {};
    const resp = await fetch("/admin/import/upload", {method:"POST", headers: uploadHeaders, body:fd});
    if (resp.status === 401) { showGate("login"); return; }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { alert("导入失败: " + (data.error||resp.status)); continue; }
    showReport(data);
  }
  loadStats(); loadKeys();
}

// 路径导入
document.getElementById("importPath").onclick = async () => {
  const path = document.getElementById("path").value.trim();
  if (!path) return alert("请输入路径");
  try {
    const r = await api("/admin/import/path", {method:"POST", headers:H(), body:JSON.stringify({path})});
    showReport(r);
    loadStats(); loadKeys();
  } catch (e) { alert("导入失败: " + e.message); }
};

// 启动：已有有效会话直接进面板，否则弹初始化/登录
(async function init() {
  if (TOKEN) {
    try {
      await api("/admin/stats", {headers: H()});
      enterPanel();
      return;
    } catch (e) { if (e.message !== "未登录") throw e; }
  }
  const status = await (await fetch("/admin/panel/status")).json();
  showGate(status.initialized ? "login" : "setup");
})();

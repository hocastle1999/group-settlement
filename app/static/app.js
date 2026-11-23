// 모임 정산 데모 페이지. 프레임워크 없이 REST API만 부른다.
// - 그룹 id와 초대 토큰은 URL의 # 뒤(fragment)에 둔다: 브라우저가 서버로 보내지 않아 접근 로그에 남지 않는다.
// - 사용자 입력은 textContent로만 화면에 넣는다(innerHTML 금지, XSS 방지).
// - 쓰기 요청마다 Idempotency-Key를 붙이고, 네트워크 오류로 응답을 못 받으면 같은 키로 다시 보낸다.
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  gid: null,
  token: null,
  group: null,
  members: [],
  currencies: [],
  actor: "",
  excluded: new Set(), // 지출을 나눌 때 뺀 멤버 id
};

// ---------------------------------------------------------------- API

async function api(method, path, body, idem) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = "Bearer " + state.token;
  if (idem) headers["Idempotency-Key"] = idem;
  if (state.actor && method !== "GET") headers["X-Actor-Member-Id"] = state.actor;
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(errorText(res.status, data));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function errorText(status, data) {
  if (Array.isArray(data.detail)) {
    // FastAPI 검증 오류: [{loc, msg}, ...]
    return data.detail.map((d) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join(", ");
  }
  return `${status} ${data.detail || "오류"}`;
}

const gpath = (rest) => `/api/groups/${state.gid}${rest}`;

/** 폼 하나의 쓰기. 응답을 못 받은 실패(네트워크)는 같은 키로 재시도할 수 있게 키를 남긴다. */
async function submitWithKey(form, send) {
  form.dataset.idem = form.dataset.idem || crypto.randomUUID();
  try {
    const result = await send(form.dataset.idem);
    delete form.dataset.idem;
    return result;
  } catch (err) {
    if (err.status) delete form.dataset.idem; // 서버가 답했다 = 다른 요청으로 다시 보내야 한다
    throw err;
  }
}

// ---------------------------------------------------------------- 화면 도우미

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function row(cells) {
  const tr = document.createElement("tr");
  for (const cell of cells) tr.append(cell instanceof Node ? cell : el("td", cell));
  return tr;
}

function num(value, signed) {
  const td = el("td", fmt(value), "num");
  if (signed && Number(value) > 0) td.classList.add("pos");
  if (signed && Number(value) < 0) td.classList.add("neg");
  return td;
}

function fmt(value) {
  const [int, frac] = String(value).split(".");
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return frac ? `${grouped}.${frac}` : grouped;
}

function button(text, onClick, className = "small") {
  const b = el("button", text, className);
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await onClick();
    } catch (err) {
      toast(err.message);
    } finally {
      b.disabled = false;
    }
  });
  return b;
}

function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.style.display = "block";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.style.display = "none"), 4000);
}

const memberName = (id) => state.members.find((m) => m.id === id)?.name ?? `#${id}`;

function tzOffset() {
  const minutes = -new Date().getTimezoneOffset();
  const sign = minutes >= 0 ? "+" : "-";
  const abs = Math.abs(minutes);
  return `${sign}${String(Math.floor(abs / 60)).padStart(2, "0")}:${String(abs % 60).padStart(2, "0")}`;
}

// ---------------------------------------------------------------- 그룹 만들기

async function showCreate() {
  $("create-view").hidden = false;
  $("group-view").hidden = true;
  fillCurrencies($("create-currency"), "KRW");
}

$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    const group = await api("POST", "/api/groups", {
      name: form.name.value,
      currency: form.currency.value,
    });
    // 토큰은 이 응답에서만 받을 수 있다. 바로 URL fragment에 넣어 둔다(북마크 = 초대 링크).
    location.hash = new URLSearchParams({ g: group.id, t: group.invite_token }).toString();
  } catch (err) {
    toast(err.message);
  }
});

// ---------------------------------------------------------------- 그룹 화면

async function showGroup() {
  $("create-view").hidden = true;
  $("group-view").hidden = false;
  state.actor = localStorage.getItem(`actor:${state.gid}`) || "";
  await reload();
}

async function reload() {
  const [group, balances, plan, entries, settlements, report, audit] = await Promise.all([
    api("GET", gpath("")),
    api("GET", gpath("/balances")),
    api("GET", gpath("/settlement")),
    api("GET", gpath("/entries?limit=30")),
    api("GET", gpath("/settlements")),
    api("GET", gpath(`/report?tz_offset=${encodeURIComponent(tzOffset())}`)),
    api("GET", gpath("/audit?limit=20")),
  ]);
  state.group = group;
  state.members = group.members;
  renderHeader(plan.version);
  renderMembers(balances);
  renderPlan(plan);
  renderEntries(entries.entries);
  renderSettlements(settlements.settlements);
  renderReport(report);
  renderAudit(audit.events);
}

function renderHeader(version) {
  $("group-name").textContent = state.group.name;
  $("group-currency").textContent = state.group.currency;
  $("group-version").textContent = `원장 버전 ${version}`;
  const actor = $("actor");
  actor.replaceChildren(el("option", "(밝히지 않음)"));
  actor.firstChild.value = "";
  for (const m of state.members) {
    const option = el("option", m.name);
    option.value = String(m.id);
    actor.append(option);
  }
  actor.value = state.actor;
  for (const select of document.querySelectorAll(".member-select")) {
    const keep = select.value;
    select.replaceChildren(...state.members.map((m) => {
      const option = el("option", m.name);
      option.value = String(m.id);
      return option;
    }));
    if (keep) select.value = keep;
  }
  // "뺀 사람"을 기억한다. "고른 사람"을 기억하면 나중에 추가한 멤버가 빠진 채로 시작한다.
  const participants = $("participants");
  participants.replaceChildren(el("span", "나눌 사람: "));
  for (const m of state.members) {
    const label = el("label", null, "check");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = String(m.id);
    box.checked = !state.excluded.has(m.id);
    box.addEventListener("change", () => {
      if (box.checked) state.excluded.delete(m.id);
      else state.excluded.add(m.id);
    });
    label.append(box, el("span", m.name));
    participants.append(label);
  }
  fillCurrencies($("expense-currency"), $("expense-currency").value || state.group.currency);
  $("fx-rate").hidden = $("expense-currency").value === state.group.currency;
}

function fillCurrencies(select, selected) {
  select.replaceChildren(...state.currencies.map((c) => {
    const option = el("option", c.code);
    option.value = c.code;
    return option;
  }));
  select.value = selected;
}

function renderMembers(balances) {
  $("balances").replaceChildren(
    ...balances.balances.map((b) => row([el("td", b.name), num(b.amount, true)])),
  );
}

function renderPlan(plan) {
  const box = $("plan");
  box.replaceChildren();
  if (plan.transfers.length === 0) {
    box.append(el("p", "보낼 돈이 없습니다.", "muted"));
  } else {
    const table = el("table");
    for (const t of plan.transfers) {
      table.append(row([`${memberName(t.sender_id)} → ${memberName(t.receiver_id)}`, num(t.amount)]));
    }
    box.append(table, el("p", `송금 ${plan.transfers.length}번 (버전 ${plan.version})`, "muted"));
  }
  $("confirm").onclick = async () => {
    try {
      await api("POST", gpath("/settlements"), { expected_version: plan.version });
      toast("정산을 확정했습니다");
    } catch (err) {
      toast(err.status === 409 && err.data.current_version !== undefined
        ? "그사이 기록이 바뀌었습니다. 새 계획을 확인하세요." : err.message);
    }
    await reload();
  };
}

const KIND = { EXPENSE: "지출", TRANSFER: "송금", REVERSAL: "취소" };

function renderEntries(entries) {
  $("entries").replaceChildren(...entries.map((e) => {
    const credit = e.lines.filter((l) => l.side === "CREDIT");
    const debit = e.lines.filter((l) => l.side === "DEBIT");
    const amount = credit.reduce((sum, l) => sum + Number(l.amount), 0);
    let text;
    if (e.kind === "EXPENSE") {
      text = `${credit.map((l) => memberName(l.member_id)).join(", ")} 결제, ` +
        `${debit.map((l) => memberName(l.member_id)).join(", ")} 부담`;
      if (e.original) text += ` (${e.original.currency} ${fmt(e.original.amount)} @ ${e.original.fx_rate})`;
    } else if (e.kind === "TRANSFER") {
      text = `${memberName(credit[0].member_id)} → ${memberName(debit[0].member_id)}`;
      if (e.settlement_id) text += ` (정산 ${e.settlement_id}의 ${e.settlement_line_no}번 완료)`;
    } else {
      text = `#${e.reverses_entry_id} 취소`;
    }
    if (e.memo) text += ` · ${e.memo}`;
    const action = e.kind !== "REVERSAL" && !e.reversed_by_entry_id
      ? button("취소", () => post(gpath(`/entries/${e.id}/reversal`), {}))
      : el("span");
    const tr = row([e.seq, KIND[e.kind], text, num(amount.toString()), el("td")]);
    tr.lastChild.append(action);
    if (e.reversed_by_entry_id) tr.classList.add("cancelled");
    return tr;
  }));
}

function renderSettlements(settlements) {
  const box = $("settlements");
  box.replaceChildren();
  if (settlements.length === 0) {
    box.textContent = "아직 없음";
    return;
  }
  for (const s of settlements) {
    const head = el("p");
    head.append(el("strong", `정산 ${s.id}`), ` 버전 ${s.version} · ${s.strategy} `);
    if (s.completed) head.append(el("span", "모두 보냄", "tag pos"));
    // 송금 완료도 분개라 버전을 올린다. 그래서 "바뀌었다"가 아니라 건수만 보여 준다.
    if (s.current_version > s.version) {
      head.append(" ", el("span", `확정 뒤 기록 ${s.current_version - s.version}건 (송금 완료 포함)`, "tag"));
    }
    const table = el("table");
    for (const t of s.transfers) {
      const status = t.completed_entry_id
        ? el("span", `보냄 (#${t.completed_entry_id})`, "pos")
        : button("보냄", () => post(gpath(`/settlements/${s.id}/transfers/${t.line_no}/complete`), {}));
      const tr = row([t.line_no, `${memberName(t.sender_id)} → ${memberName(t.receiver_id)}`,
        num(t.amount), el("td")]);
      tr.lastChild.append(status);
      table.append(tr);
    }
    box.append(head, table);
  }
}

function renderReport(report) {
  $("report-members").replaceChildren(...report.members.map((m) =>
    row([m.name, num(m.paid), num(m.share), num(m.sent), num(m.received), num(m.balance, true)])));
  $("report-daily").replaceChildren(...report.daily.map((d) =>
    row([d.day, `${d.expenses}건`, num(d.total), num(d.cumulative)])));
  $("report-currency").replaceChildren(...report.by_currency.map((c) =>
    row([c.currency, `${c.expenses}건`, num(c.original_total), num(c.converted_total)])));
}

function renderAudit(events) {
  $("audit").replaceChildren(...events.map((e) => row([
    e.seq,
    new Date(e.created_at).toLocaleString(),
    e.action,
    e.actor_member_id ? memberName(e.actor_member_id) : "",
    el("code", e.hash.slice(0, 12)),
  ])));
}

async function post(path, body) {
  await api("POST", path, body, crypto.randomUUID());
  await reload();
}

// ---------------------------------------------------------------- 폼

$("actor").addEventListener("change", (event) => {
  state.actor = event.target.value;
  localStorage.setItem(`actor:${state.gid}`, state.actor);
});

$("copy-link").addEventListener("click", async () => {
  await navigator.clipboard.writeText(location.href);
  toast("초대 링크를 복사했습니다");
});

$("expense-currency").addEventListener("change", (event) => {
  $("fx-rate").hidden = event.target.value === state.group.currency;
});

$("member-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("POST", gpath("/members"), { name: event.target.name.value });
    event.target.reset();
    await reload();
  } catch (err) {
    toast(err.message);
  }
});

$("expense-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const ids = [...$("participants").querySelectorAll("input:checked")].map((i) => Number(i.value));
  const body = {
    payer_id: Number(form.payer.value),
    amount: form.amount.value.trim(),
    split: { type: "equal", member_ids: ids },
    memo: form.memo.value,
  };
  if (form.currency.value !== state.group.currency) {
    body.currency = form.currency.value;
    body.fx_rate = form.fx_rate.value.trim();
  }
  try {
    await submitWithKey(form, (key) => api("POST", gpath("/expenses"), body, key));
    form.amount.value = "";
    form.memo.value = "";
    await reload();
  } catch (err) {
    toast(err.message);
  }
});

$("transfer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = {
    sender_id: Number(form.sender.value),
    receiver_id: Number(form.receiver.value),
    amount: form.amount.value.trim(),
  };
  try {
    await submitWithKey(form, (key) => api("POST", gpath("/transfers"), body, key));
    form.amount.value = "";
    await reload();
  } catch (err) {
    toast(err.message);
  }
});

$("verify").addEventListener("click", async () => {
  const result = await api("GET", gpath("/audit/verify"));
  $("verify-result").textContent = result.valid
    ? `이상 없음 (${result.events}줄)`
    : `변조 감지: ${result.broken_at_seq}번 - ${result.reason}`;
});

// ---------------------------------------------------------------- 시작

async function route() {
  const params = new URLSearchParams(location.hash.slice(1));
  state.gid = params.get("g");
  state.token = params.get("t");
  try {
    if (state.gid && state.token) await showGroup();
    else await showCreate();
  } catch (err) {
    toast(err.status === 404 ? "그룹을 찾을 수 없습니다 (링크를 확인하세요)" : err.message);
    await showCreate();
  }
}

window.addEventListener("hashchange", route);
api("GET", "/api/currencies").then((list) => {
  state.currencies = list.currencies;
  route();
});

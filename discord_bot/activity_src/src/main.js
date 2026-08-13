import { DiscordSDK } from "@discord/embedded-app-sdk";
import "./style.css";

const ranges = ["Нулевая", "Ближняя", "Средняя", "Дальняя"];
const app = document.querySelector("#app");
let discordSdk;
let auth;
let state = { selectedRange: "Средняя", selectedItem: null };

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;");

function meter(current, max, danger = false) {
  const value = Math.max(0, Math.min(100, (current / Math.max(1, max)) * 100));
  return `<div class="meter ${danger ? "danger" : ""}"><i style="width:${value}%"></i></div>`;
}

function itemLine(item) {
  const durability = item.max_durability ? ` · ${item.durability}/${item.max_durability}` : "";
  const ammo = item.ammo_max ? ` · ${item.ammo}/${item.ammo_max} патр.` : "";
  return `<button class="item" data-item="${item.id}">
    <span><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.category)}${durability}${ammo}</small></span>
    <em>${item.equipped ? "СНАРЯЖЕНО" : "В РЮКЗАКЕ"}</em>
  </button>`;
}

function render(data) {
  const c = data.character;
  const attributeHtml = Object.entries(c.attributes).map(([name, value]) => `
    <div class="stat">
      <div><span>${escapeHtml(name)}</span><b>${value.current} / ${value.max}</b></div>
      ${meter(value.current, value.max, value.current <= Math.ceil(value.max / 3))}
    </div>`).join("");
  const injuries = c.injuries.length
    ? c.injuries.map(i => `<article class="wound"><b>${escapeHtml(i.name)}</b><span>${escapeHtml(i.penalties || i.description)}</span></article>`).join("")
    : '<p class="muted">Активных травм нет</p>';
  const equipment = data.equipped.length
    ? data.equipped.map(itemLine).join("")
    : '<p class="muted">Ничего не экипировано</p>';

  app.innerHTML = `
    <header>
      <div class="brand"><span class="sigil">RR</span><div><b>RATTEN REICH</b><small>ПОЛЕВОЙ КОМАНДНЫЙ ТЕРМИНАЛ</small></div></div>
      <div class="connection"><i></i> СВЯЗЬ УСТАНОВЛЕНА</div>
    </header>
    <main>
      <aside class="dossier panel">
        <div class="eyebrow">ЛИЧНОЕ ДЕЛО №${c.id}</div>
        <h1>${escapeHtml(c.surname)}<br><strong>${escapeHtml(c.name)}</strong></h1>
        <p class="identity">${escapeHtml(c.race)} · ${escapeHtml(c.className)}</p>
        <div class="rule"></div>
        ${attributeHtml}
        <div class="vitals">
          <div><span>ВОЛЯ</span><b>${c.will.current}/${c.will.max}</b>${meter(c.will.current, c.will.max)}</div>
          <div><span>ЗАРАЖЕНИЕ</span><b>${c.infection}/5</b>${meter(c.infection, 5, true)}</div>
        </div>
        <div class="supply"><span>БЛАНКИ СНАБЖЕНИЯ</span><b>${c.supplyForms}</b></div>
      </aside>
      <section class="battle panel">
        <div class="section-head"><div><span>ТАКТИЧЕСКАЯ СХЕМА</span><h2>ЛИНИЯ СОПРИКОСНОВЕНИЯ</h2></div><button id="center">ЦЕНТРИРОВАТЬ</button></div>
        <div class="range-map">
          ${ranges.map((range, index) => `
            <button class="range ${state.selectedRange === range ? "active" : ""}" data-range="${range}">
              <span>0${index + 1}</span><b>${range.toUpperCase()}</b><small>${["РУКОПАШНАЯ", "ПИСТОЛЕТЫ И ДРОБОВИКИ", "ОСНОВНАЯ ДИСТАНЦИЯ", "ПРЕДЕЛЬНЫЙ ОГОНЬ"][index]}</small>
              ${state.selectedRange === range ? '<i class="unit">ВАШ ОТРЯД</i>' : ""}
            </button>`).join("")}
        </div>
        <div class="action-bar">
          <div><span>ВЫБРАННАЯ ЗОНА</span><b id="range-name">${state.selectedRange}</b></div>
          <button class="primary" id="prepare">ПОДГОТОВИТЬ ДЕЙСТВИЕ</button>
        </div>
        <p class="prototype-note">Боевой стол подключён к персонажу. Перемещение жетонов и синхронные атаки появятся на следующем этапе.</p>
      </section>
      <aside class="loadout panel">
        <nav><button class="tab active" data-tab="equipment">СНАРЯЖЕНИЕ</button><button class="tab" data-tab="wounds">ТРАВМЫ</button></nav>
        <div id="equipment" class="tab-body active">${equipment}</div>
        <div id="wounds" class="tab-body">${injuries}</div>
        <footer><span>АКТИВНЫЕ ЭФФЕКТЫ</span><b>${data.effects.length}</b><span>ТАЛАНТЫ</span><b>${Object.keys(c.talents).length}</b></footer>
      </aside>
    </main>`;

  document.querySelectorAll("[data-range]").forEach(button => button.addEventListener("click", () => {
    state.selectedRange = button.dataset.range;
    render(data);
  }));
  document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".tab,.tab-body").forEach(node => node.classList.remove("active"));
    button.classList.add("active");
    document.querySelector(`#${button.dataset.tab}`).classList.add("active");
  }));
  document.querySelectorAll("[data-item]").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".item").forEach(node => node.classList.remove("selected"));
    button.classList.add("selected");
    state.selectedItem = button.dataset.item;
  }));
  document.querySelector("#prepare").addEventListener("click", () => {
    const button = document.querySelector("#prepare");
    button.textContent = "ДЕЙСТВИЕ ЗАПИСАНО";
    setTimeout(() => button.textContent = "ПОДГОТОВИТЬ ДЕЙСТВИЕ", 1200);
  });
}

function showError(message) {
  app.innerHTML = `<div class="fatal"><span>RR</span><h1>НЕТ СВЯЗИ СО ШТАБОМ</h1><p>${escapeHtml(message)}</p><button onclick="location.reload()">ПОВТОРИТЬ ПОДКЛЮЧЕНИЕ</button></div>`;
}

async function boot() {
  const configResponse = await fetch("/.proxy/api/config");
  const config = await configResponse.json();
  if (!config.configured) throw new Error("На сервере не настроены DISCORD_CLIENT_ID и DISCORD_CLIENT_SECRET.");
  discordSdk = new DiscordSDK(config.clientId);
  await discordSdk.ready();
  const { code } = await discordSdk.commands.authorize({
    client_id: config.clientId,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify"],
  });
  const tokenResponse = await fetch("/.proxy/api/token", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({code}),
  });
  if (!tokenResponse.ok) throw new Error(await tokenResponse.text());
  const token = await tokenResponse.json();
  auth = await discordSdk.commands.authenticate({access_token: token.access_token});
  if (!auth) throw new Error("Discord не подтвердил пользователя.");
  const guildId = discordSdk.guildId;
  if (!guildId) throw new Error("Откройте игру внутри канала сервера.");
  const response = await fetch(`/.proxy/api/character?guild_id=${guildId}`, {
    headers: {Authorization: `Bearer ${token.access_token}`},
  });
  if (!response.ok) throw new Error(await response.text());
  render(await response.json());
}

boot().catch(error => {
  console.error(error);
  showError(error.message || "Неизвестная ошибка");
});
"use strict";

// Agent mascots, the same shapes and colours as in the app.
const SVGNS = "http://www.w3.org/2000/svg";
const MASCOT = {
  intake: { color: "#3B82F6", eyes: [15.5, 24.5, 20], body: '<circle cx="20" cy="20" r="16"/>' },
  fulfillment: { color: "#F59E0B", eyes: [15.5, 24.5, 20], body: '<rect x="4.5" y="4.5" width="31" height="31" rx="9"/>' },
  credit: { color: "#8B5CF6", eyes: [15.5, 24.5, 20],
    body: '<path d="M20 4.5l13.4 7.75v15.5L20 35.5 6.6 27.75v-15.5z" stroke="#8B5CF6" stroke-width="4" stroke-linejoin="round"/>' },
  collection: { color: "#F43F5E", eyes: [16.5, 23.5, 20], body: '<rect x="8.5" y="8.5" width="23" height="23" rx="6.5" transform="rotate(45 20 20)"/>' },
  cash_application: { color: "#10B981", eyes: [16, 24, 20],
    body: '<circle cx="20" cy="11" r="8.5"/><circle cx="29" cy="20" r="8.5"/><circle cx="20" cy="29" r="8.5"/><circle cx="11" cy="20" r="8.5"/><circle cx="20" cy="20" r="9"/>' },
  supervisor: { color: "#6366F1", eyes: [15.5, 24.5, 21],
    body: '<path d="M5.5 33V19a14.5 14.5 0 0 1 29 0v14a2.5 2.5 0 0 1-2.5 2.5H8A2.5 2.5 0 0 1 5.5 33z"/>' },
};

function mascot(agent, size) {
  const m = MASCOT[agent];
  const [lx, rx, y] = m.eyes;
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("viewBox", "0 0 40 40");
  s.setAttribute("width", size);
  s.setAttribute("height", size);
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = `<g fill="${m.color}">${m.body}</g>`
    + `<ellipse cx="${lx}" cy="${y}" rx="2.6" ry="3.6" fill="#0F172A"/><ellipse cx="${rx}" cy="${y}" rx="2.6" ry="3.6" fill="#0F172A"/>`;
  return s;
}

document.querySelectorAll("[data-mascot]").forEach((el) => {
  const size = el.closest(".flow-node") ? (el.closest(".pair") ? 28 : 40) : 28;
  el.append(mascot(el.dataset.mascot, size));
});
document.querySelectorAll("[data-mascots]").forEach((el) => {
  Object.keys(MASCOT).forEach((k) => el.append(mascot(k, 54)));
});

// Each demo plays its steps once, in order, when it scrolls into view; "Putar ulang" replays it.
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const GAP = { hero: 950, flow: 420, plan: 700, ask: 900, inject: 1100, hold: 800, desk: 700, chain: 900 };
const timers = new Map();

function groups(box) {
  const byStep = new Map();
  box.querySelectorAll(".step").forEach((el) => {
    const n = Number(el.dataset.step);
    if (!byStep.has(n)) byStep.set(n, []);
    byStep.get(n).push(el);
  });
  return [...byStep.entries()].sort((a, b) => a[0] - b[0]);
}

function reset(box) {
  (timers.get(box) || []).forEach(clearTimeout);
  timers.set(box, []);
  box.querySelectorAll(".on, .pressed, .active").forEach((el) => el.classList.remove("on", "pressed", "active"));
  box.classList.remove("on");
}

function play(box) {
  const name = box.dataset.play;
  reset(box);
  if (reduce) {
    box.classList.add("on");
    box.querySelectorAll(".step").forEach((el) => el.classList.add("on"));
    box.querySelectorAll("[data-press]").forEach((el) => el.classList.add("pressed"));
    return;
  }
  const gap = GAP[name] || 800;
  const later = (ms, fn) => timers.get(box).push(setTimeout(fn, ms));
  box.classList.add("on");
  if (name === "desk") {
    // the approval card is already on screen: press "Setujui", then show the outcome
    later(700, () => box.querySelector("[data-press]").classList.add("pressed"));
    later(700 + gap, () => box.querySelectorAll(".step").forEach((el) => el.classList.add("on")));
    return;
  }
  groups(box).forEach(([n, els], i) => {
    later(250 + i * gap, () => {
      els.forEach((el) => el.classList.add("on"));
      if (name === "chain" && n === 2) {
        box.querySelector('.chain-item[data-step="2"]').classList.add("active");
      }
    });
  });
}

const seen = new IntersectionObserver((entries) => {
  entries.forEach((e) => {
    if (!e.isIntersecting) return;
    e.target.classList.add("seen");
    if (e.target.dataset.play && !e.target.dataset.played) {
      e.target.dataset.played = "1";
      play(e.target);
    }
    seen.unobserve(e.target);
  });
}, { threshold: 0.3 });
document.querySelectorAll(".reveal").forEach((el) => seen.observe(el));

document.querySelectorAll("[data-replay]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const box = document.querySelector(`[data-play="${btn.dataset.replay}"]`);
    if (box) play(box);
  });
});

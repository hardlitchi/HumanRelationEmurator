// 小さな補助だけ。JS がなくても全操作できる（サーバー側で型を解釈する）。
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-confirm]");
  if (btn && !confirm(btn.dataset.confirm)) ev.preventDefault();
});

document.querySelectorAll("[data-select]").forEach((el) => el.addEventListener("focus", () => el.select()));

// 主張フォーム: 選んだフィールドの型に合わせて入力欄を切り替える
document.querySelectorAll(".fact-form").forEach((form) => {
  const field = form.querySelector("select[name=field]");
  const plain = form.querySelector(".v-plain");
  const enumBox = form.querySelector(".v-enum");
  const entityBox = form.querySelector(".v-entity");
  const desc = form.querySelector(".field-desc");
  const show = (box, on) => {
    box.hidden = !on;
    box.querySelectorAll("input, select").forEach((el) => (el.disabled = !on));
  };
  const update = () => {
    const opt = field.selectedOptions[0];
    const type = (opt && opt.dataset.type) || "text";
    desc.textContent = (opt && opt.dataset.desc) || "";
    const choices = type === "boolean" ? ["true", "false"]
      : type === "enum" ? (opt.dataset.options || "").split("|").filter(Boolean) : null;
    show(plain, !choices && type !== "entity_ref");
    show(enumBox, !!choices);
    show(entityBox, type === "entity_ref");
    if (choices) {
      const sel = enumBox.querySelector("select");
      sel.replaceChildren(...choices.map((c) => new Option(c === "true" ? "はい" : c === "false" ? "いいえ" : c, c)));
    }
    const input = plain.querySelector("input");
    input.type = { number: "number", integer: "number", url: "url", email: "email" }[type] || "text";
    input.step = type === "number" ? "any" : "1";
    input.placeholder = { date: "YYYY-MM-DD（年不明なら --MM-DD）", datetime: "2026-01-31T20:00+09:00",
                          json: '{"key": "value"}' }[type] || "";
  };
  field.addEventListener("change", update);
  update();
});

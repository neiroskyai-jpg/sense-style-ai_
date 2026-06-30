/* Всплывающий чат-стилист: плавающая кнопка → попап с чатом. Подключается одной строкой:
   <script src="/stylist-widget.js" defer></script>
   Работает на любой странице того же домена; шлёт в /stylist/msg (контекст — Формула вошедшей). */
(function () {
  if (window.__ssStylistWidget) return;
  window.__ssStylistWidget = true;

  var css =
    '.ssw-btn{position:fixed;right:20px;bottom:20px;z-index:99998;width:auto;max-width:none;display:inline-flex;align-items:center;gap:9px;' +
    'background:#7A1C2E;color:#fff;border:0;border-radius:999px;padding:13px 20px;font-family:Georgia,serif;' +
    'font-size:15px;cursor:pointer;box-shadow:0 6px 22px rgba(0,0,0,.22)}' +
    '.ssw-btn:hover{opacity:.93}.ssw-btn svg{width:18px;height:18px}' +
    '.ssw-panel{position:fixed;right:20px;bottom:20px;z-index:99999;width:370px;max-width:calc(100vw - 32px);' +
    'height:560px;max-height:calc(100vh - 40px);background:#F5EFE3;border:1px solid #e3dccf;border-radius:16px;' +
    'display:none;flex-direction:column;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.28);font-family:Georgia,serif}' +
    '.ssw-head{display:flex;justify-content:space-between;align-items:center;padding:13px 16px;background:#fff;' +
    'border-bottom:1px solid #e3dccf}.ssw-head b{font-weight:normal;font-size:16px;color:#1f1d1b}' +
    '.ssw-x{background:none;border:0;font-size:22px;color:#6b645c;cursor:pointer;line-height:1}' +
    '.ssw-feed{flex:1;overflow-y:auto;padding:16px}.ssw-m{margin:8px 0;display:flex}' +
    '.ssw-m .b{padding:10px 13px;border-radius:13px;font-size:14.5px;line-height:1.5;max-width:84%}' +
    '.ssw-m.u{justify-content:flex-end}.ssw-m.u .b{background:#7A1C2E;color:#fff;border-bottom-right-radius:4px}' +
    '.ssw-m.a .b{background:#fff;border:1px solid #e3dccf;color:#1f1d1b;border-bottom-left-radius:4px}' +
    '.ssw-t{color:#6b645c;font-size:13px;padding:2px 4px}' +
    '.ssw-bar{border-top:1px solid #e3dccf;background:#fff;padding:10px;display:flex;gap:8px}' +
    '.ssw-bar textarea{flex:1;resize:none;border:1px solid #d9d2c7;border-radius:10px;padding:9px 11px;' +
    'font:inherit;font-size:14px;height:40px;max-height:90px}' +
    '.ssw-bar button{flex:none;width:auto;background:#7A1C2E;color:#fff;border:0;border-radius:10px;padding:0 16px;font-size:16px;cursor:pointer}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var btn = document.createElement('button');
  btn.className = 'ssw-btn';
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7A8.5 8.5 0 1 1 21 11.5z"/></svg><span>Спросить стилиста</span>';

  var panel = document.createElement('div');
  panel.className = 'ssw-panel';
  panel.innerHTML =
    '<div class=ssw-head><b>Стилист · Чувство стиля</b><button class=ssw-x aria-label="Закрыть">&times;</button></div>' +
    '<div class=ssw-feed></div>' +
    '<div class=ssw-bar><textarea placeholder="Что надеть на встречу? с чего начать?"></textarea><button aria-label="Отправить">&#8594;</button></div>';

  document.body.appendChild(btn);
  document.body.appendChild(panel);

  var feed = panel.querySelector('.ssw-feed');
  var inp = panel.querySelector('textarea');
  var sendBtn = panel.querySelector('.ssw-bar button');
  var history = [];

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function add(role, text) {
    var d = document.createElement('div');
    d.className = 'ssw-m ' + (role === 'user' ? 'u' : 'a');
    d.innerHTML = '<div class=b>' + esc(text).replace(/\n/g, '<br>') + '</div>';
    feed.appendChild(d); feed.scrollTop = feed.scrollHeight; return d;
  }
  function send() {
    var t = inp.value.trim(); if (!t) return;
    inp.value = ''; add('user', t); history.push({ role: 'user', content: t });
    var tip = document.createElement('div'); tip.className = 'ssw-m a';
    tip.innerHTML = '<div class="b ssw-t">печатает…</div>'; feed.appendChild(tip); feed.scrollTop = feed.scrollHeight;
    fetch('/stylist/msg', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ history: history }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        tip.remove();
        var rep = d.reply || 'Не получилось ответить, попробуй ещё раз.';
        add('assistant', rep); history.push({ role: 'assistant', content: rep });
      })
      .catch(function () { tip.remove(); add('assistant', 'Связь прервалась — попробуй ещё раз.'); });
  }
  function open() {
    panel.style.display = 'flex'; btn.style.display = 'none';
    if (!history.length) add('assistant', 'Привет. Я твой стилист. Помогу одеваться так, чтобы тебя считывали той, кем ты себя ощущаешь — а не той, кем привыкли видеть. С чем хочешь разобраться?');
    inp.focus();
  }
  function close() { panel.style.display = 'none'; btn.style.display = 'flex'; }

  btn.addEventListener('click', open);
  panel.querySelector('.ssw-x').addEventListener('click', close);
  sendBtn.addEventListener('click', send);
  inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
})();

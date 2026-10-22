// Prévia das páginas no topo (22/10/2026) - ver previas.css.
// As maquetes vêm de /static/topo/previas.html, buscado uma vez só, no
// primeiro passar de mouse no menu (ou quando o navegador estiver ocioso).
(function () {
  if (!matchMedia('(hover: hover)').matches) return;          // celular: sem prévia
  const nav = document.querySelector('.topo-nav'), prev = document.getElementById('prev');
  if (!nav || !prev) return;
  const palco = prev.querySelector('.prev-palco');
  const links = [...nav.querySelectorAll('.topo-link[data-prev]')];
  let modelos = null, carregando = null, atual = null, tAbrir = 0, tFechar = 0;
  document.body.classList.add('previas-prontas');

  function carregar() {
    if (modelos || carregando) return carregando;
    carregando = fetch('/static/topo/previas.html', { credentials: 'same-origin' })
      .then(r => r.ok ? r.text() : Promise.reject(r.status))
      .then(t => { const d = document.createElement('div'); d.innerHTML = t; modelos = d; })
      .catch(() => { carregando = null; });
    return carregando;
  }
  if ('requestIdleCallback' in window) requestIdleCallback(() => carregar(), { timeout: 4000 });

  function camada(link) {
    const tp = modelos && modelos.querySelector('#tp-' + link.dataset.prev);
    if (!tp) return null;
    const c = document.createElement('div');
    c.className = 'prev-camada';
    c.innerHTML = '<div class="prev-cab"><b></b><span></span><a></a></div><div class="prev-tela"><div class="prev-escala"></div></div>';
    c.querySelector('b').textContent = tp.dataset.titulo;
    c.querySelector('.prev-cab span').textContent = tp.dataset.desc;
    const a = c.querySelector('a'); a.href = link.href; a.textContent = 'Abrir ' + tp.dataset.titulo;
    c.querySelector('.prev-escala').innerHTML = tp.innerHTML;
    c.addEventListener('click', () => { location.href = link.href; });
    return c;
  }
  function escalar(c) { const t = c.querySelector('.prev-tela'); t.style.setProperty('--esc', (t.clientWidth / 560).toFixed(3)); }
  function posicionar(link) {
    const r = link.getBoundingClientRect(), W = prev.offsetWidth;
    const left = Math.max(12, Math.min((innerWidth - W) / 2, innerWidth - W - 12));
    prev.style.left = (left + scrollX) + 'px';
    prev.style.top = (r.bottom + scrollY + 14) + 'px';
  }
  async function abrir(link) {
    if (link.classList.contains('topo-ativo')) { fechar(); return; }
    if (!modelos) { await carregar(); if (!modelos || !link.matches(':hover, :focus-visible')) return; }
    const k = link.dataset.prev, aberto = prev.classList.contains('aberto');
    if (aberto && atual === k) return;
    const nova = camada(link); if (!nova) return;
    links.forEach(l => l.classList.toggle('com-previa', l === link));
    const velha = palco.querySelector('.prev-camada:not(.saindo)');
    if (velha && aberto) {
      nova.classList.add('entrando'); palco.appendChild(nova); velha.classList.add('saindo');
      void nova.offsetWidth; nova.classList.remove('entrando'); setTimeout(() => velha.remove(), 260);
    } else { palco.replaceChildren(nova); }
    if (!aberto) { prev.style.transition = 'none'; posicionar(link); void prev.offsetWidth; prev.style.transition = ''; }
    escalar(nova);
    atual = k;
    prev.classList.add('aberto'); prev.removeAttribute('aria-hidden');
    document.body.classList.add('escurece');
  }
  function fechar() {
    prev.classList.remove('aberto'); prev.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('escurece'); links.forEach(l => l.classList.remove('com-previa')); atual = null;
  }
  links.forEach(link => {
    link.addEventListener('mouseenter', () => { carregar(); clearTimeout(tFechar); clearTimeout(tAbrir);
      tAbrir = setTimeout(() => abrir(link), prev.classList.contains('aberto') ? 0 : 90); });
    link.addEventListener('mouseleave', () => { clearTimeout(tAbrir); tFechar = setTimeout(fechar, 180); });
    link.addEventListener('focus', () => { if (link.matches(':focus-visible')) abrir(link); });
    link.addEventListener('blur', () => { tFechar = setTimeout(fechar, 180); });
  });
  prev.addEventListener('mouseenter', () => clearTimeout(tFechar));
  prev.addEventListener('mouseleave', () => { tFechar = setTimeout(fechar, 180); });
  addEventListener('keydown', e => { if (e.key === 'Escape') fechar(); });
  addEventListener('resize', () => { const l = nav.querySelector('.com-previa'); if (l) { posicionar(l); palco.querySelectorAll('.prev-camada').forEach(escalar); } });
})();

// Gmail draft helper for Pedidos
function openGmailDraft(to, subject, body){
  const url = `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(to||'')}&su=${encodeURIComponent(subject||'')}&body=${encodeURIComponent(body||'')}`;
  window.open(url, '_blank');
}

(function(){
  const btn = document.getElementById('gmail-draft-btn');
  if(btn){
    btn.addEventListener('click', ()=>{
      const cliente = document.getElementById('pedido_cliente')?.value||'';
      const sucursal = document.getElementById('pedido_sucursal')?.value||'';
      const mail = document.getElementById('pedido_mail_pedido')?.value||'';
      const nota = document.getElementById('pedido_nota')?.value||'';
      const desc = document.getElementById('pedido_descripcion')?.value||'';
      const subject = `Pedido a ${cliente}${sucursal? ' | Sucursal: '+sucursal:''}`;
      const body = `Nota:\n${nota}\n\nDescripción del pedido:\n${desc}\n\nAtentamente,\nSirio Arabia`;
      openGmailDraft(mail, subject, body);
    });
  }
})();

// Generic: Table -> Cards (mobile)
(function(){
  const MQ = 768; // <768 mobile
  function isMobilePortrait(){
    try { return window.innerWidth < MQ && window.matchMedia && window.matchMedia('(orientation: portrait)').matches; }
    catch(e){ return window.innerWidth < MQ; }
  }

  function buildCardsFor(wrapper){
    try{
      const table = wrapper.querySelector('table');
      if(!table) return;
      // find/create sibling .mobile-cards
      let cards = wrapper.nextElementSibling;
      if(!cards || !cards.classList.contains('mobile-cards')){
        cards = document.createElement('div');
        cards.className = 'mobile-cards';
        wrapper.parentNode.insertBefore(cards, wrapper.nextSibling);
      }
      cards.innerHTML = '';
      // headers
      const heads = Array.from(table.querySelectorAll('thead th')).map(th => (th.textContent||'').trim());
      const bodyRows = Array.from(table.querySelectorAll('tbody tr'));
      bodyRows.forEach(tr => {
        const tds = Array.from(tr.children);
        if(!tds.length) return;
        // Heurística de título: primera col no vacía, o que contenga Cliente/Apellido/Nombre/Empresa
        let titleIdx = 0;
        const preferred = ['Razón social','Razon social','Razón','Razon','Empresa','Cliente','Apellido','Nombre','Marca','Título','Titulo'];
        for(let i=0;i<heads.length;i++){
          if(preferred.some(p => (heads[i]||'').toLowerCase().includes(p.toLowerCase()))){ titleIdx = i; break; }
        }
        const card = document.createElement('div');
        card.className = 'mc-card';
        const header = document.createElement('div');
        header.className = 'mc-header';
        const title = document.createElement('div');
        title.className = 'mc-title';
        title.innerHTML = (tds[titleIdx] && tds[titleIdx].innerHTML) || (tds[0]?.innerHTML||'');
        header.appendChild(title);
        card.appendChild(header);

        const body = document.createElement('div');
        body.className = 'mc-body';

        // Detect celda de acciones (última con botones)
        let actionsHTML = '';
        const lastTd = tds[tds.length-1];
        if(lastTd && (lastTd.querySelector('.btn') || lastTd.classList.contains('text-end'))){
          actionsHTML = lastTd.innerHTML;
        }

        const rows = [];
        for(let i=0;i<tds.length;i++){
          // omitir título duplicado y acciones
          if(i===titleIdx) continue;
          if(i===tds.length-1 && actionsHTML) continue;
          const label = heads[i] || '';
          const val = tds[i].innerHTML;
          // saltar si está vacío
          const txt = (tds[i].textContent||'').trim();
          if(!txt && !tds[i].querySelector('input,select,button,svg,img')) continue;
          const isWide = tds[i].classList.contains('col-money') || tds[i].classList.contains('col-forma') || !!tds[i].querySelector('.js-price-group,.js-cob-price-group,.form-control,.form-select');
          rows.push({label, val, isWide});
        }

        // mostrar 4 principales y colapsar resto
        const primaryCount = Math.min(4, rows.length);
        rows.forEach((r, idx)=>{
          const row = document.createElement('div');
          row.className = 'mc-row' + (r.isWide ? ' mc-row-wide' : '') + (idx>=primaryCount ? ' mc-row-extra d-none' : '');
          const l = document.createElement('div'); l.className='mc-label'; l.textContent = r.label || '';
          const v = document.createElement('div'); v.className='mc-value'; v.innerHTML = r.val || '-';
          row.appendChild(l); row.appendChild(v);
          body.appendChild(row);
        });
        card.appendChild(body);

        if(rows.length > primaryCount){
          const more = document.createElement('div');
          more.className = 'mc-more';
          const btn = document.createElement('button');
          btn.type='button'; btn.className='mc-more-toggle'; btn.textContent='Ver más';
          btn.addEventListener('click', function(){
            const hidden = card.querySelectorAll('.mc-row-extra');
            const isHidden = hidden.length && hidden[0].classList.contains('d-none');
            hidden.forEach(n=> n.classList.toggle('d-none'));
            btn.textContent = isHidden ? 'Ver menos' : 'Ver más';
          });
          more.appendChild(btn);
          card.appendChild(more);
        }

        if(actionsHTML){
          const actions = document.createElement('div');
          actions.className = 'mc-actions';
          actions.innerHTML = actionsHTML;
          card.appendChild(actions);
        }

        cards.appendChild(card);
      });
    } catch(e){ /* noop */ }
  }

  function rebuild(){
    const wrappers = document.querySelectorAll('.desktop-table');
    wrappers.forEach(w => {
      const cards = w.nextElementSibling && w.nextElementSibling.classList.contains('mobile-cards') ? w.nextElementSibling : null;
      if(isMobilePortrait()) buildCardsFor(w);
      else if(cards) cards.innerHTML = '';
    });
  }

  function init(){ rebuild(); }
  window.addEventListener('DOMContentLoaded', init);
  window.addEventListener('resize', function(){ clearTimeout(window.__mc_to); window.__mc_to = setTimeout(rebuild, 150); });
})();

(function(){
  // Los menus de acciones de las tablas viven dentro de .table-responsive, que por el
  // overflow-x de Bootstrap clippea tambien en vertical: por spec CSS, overflow-y:visible
  // se computa a auto cuando el otro eje no es visible. Posicionar el menu contra el
  // viewport lo saca de ese recorte sin tocar el scroll horizontal de la tabla.
  function fijarMenusDeTabla(){
    if(!(window.bootstrap && window.bootstrap.Dropdown)) return;
    const toggles = document.querySelectorAll('.table-responsive [data-bs-toggle="dropdown"]');
    toggles.forEach(t => {
      window.bootstrap.Dropdown.getOrCreateInstance(t, {
        popperConfig: base => Object.assign({}, base, { strategy: 'fixed' })
      });
    });
  }
  window.addEventListener('DOMContentLoaded', fijarMenusDeTabla);
})();

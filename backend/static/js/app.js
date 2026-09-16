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

  /* Si el CSS oculta el contenedor, no se construyen las tarjetas.
     ─────────────────────────────────────────────────────────────────────────
     /clientes y /empresas declaran `<div class="mobile-cards">` en el template
     pero eligen tabla con scroll horizontal, asi que app.css lo deja siempre en
     `display:none` (`body[data-active="clientes"] .mobile-cards` y su gemela de
     empresas). Igual se construian ~80 tarjetas en cada carga y en cada resize,
     para nada.

     Y no era solo CPU: `buildCardsFor` copia `lastTd.innerHTML` (la celda de
     acciones) dentro de la tarjeta, y en estas dos vistas esa celda contiene
     los modales `archiveCompany{id}` / `archiveClient{id}`. O sea que
     duplicaba ~80 modales, con sus ids, en DOM oculto.

     Se pregunta por el computed style y no por `data-active` a proposito: asi
     vale para cualquier vista que decida ocultar las tarjetas, sin tener que
     mantener una lista. Si el contenedor todavia no existe (`cards === null`)
     se construye como siempre: ahi el que decide es `isMobilePortrait()`.      */
  function cardsOcultas(cards){
    try { return window.getComputedStyle(cards).display === 'none'; }
    catch(e){ return false; }
  }

  function rebuild(){
    const wrappers = document.querySelectorAll('.desktop-table');
    wrappers.forEach(w => {
      const cards = w.nextElementSibling && w.nextElementSibling.classList.contains('mobile-cards') ? w.nextElementSibling : null;
      if(!isMobilePortrait()){
        if(cards) cards.innerHTML = '';
        return;
      }
      if(cards && cardsOcultas(cards)){
        if(cards.innerHTML) cards.innerHTML = '';
        return;
      }
      buildCardsFor(w);
    });
  }

  function init(){ rebuild(); }
  window.addEventListener('DOMContentLoaded', init);
  window.addEventListener('resize', function(){ clearTimeout(window.__mc_to); window.__mc_to = setTimeout(rebuild, 150); });
})();

/* ═══════════════════════════════════════════════════════════════════════════
   D — UN SOLO PUNTO DE "EL LISTADO CAMBIO" (window.SirioList)
   ───────────────────────────────────────────────────────────────────────────
   NO BORRAR. Los listados de /empresas y /clientes se repintan por AJAX:
   `replaceListFromHtml()` hace `tbody.innerHTML = nuevoTbody.innerHTML` en cada
   busqueda y en cada paginacion. Eso DESTRUYE los nodos viejos, y con ellos
   todo lo que estuviera inicializado sobre esos nodos.

   Sintoma medido que motivo esto (era el bug del menu de 3 puntos que "volvia"):
     antes del AJAX  -> 10/10 toggles con popperConfig strategy:'fixed'
     tecleo una letra en el buscador
     despues del AJAX ->  0/10. Bootstrap crea instancias default al primer
                          click, el menu vuelve a position:absolute y lo
                          clippea el .table-responsive (overflow:auto).
   O sea: el fix del commit b1c539a no "volvia a romperse", nunca sobrevivia a
   la primera interaccion, porque se aplicaba solo en DOMContentLoaded.

   POR QUE MutationObserver Y NO UN HOOK EXPLICITO
   El hook explicito es mas predecible, pero hay que acordarse de llamarlo en
   CADA lugar que muta filas, y en este repo son varios y de distinta forma:
   `replaceListFromHtml` (innerHTML), el ordenamiento de columnas (reordena con
   `tbody.appendChild(tr)`, que no crea nodos pero si cambia el orden), y los
   toggles de alertas. El observer los agarra a todos sin tener que cazarlos uno
   por uno, y sobre todo agarra al que alguien agregue el mes que viene sin
   leer este comentario.
   El riesgo conocido del observer (dispararse de mas) se acota con tres cosas:
     1) se observa SOLO el <tbody>, no el contenedor. Las cards se escriben
        AFUERA de la tabla, asi que un consumidor no puede realimentar al
        observer que lo desperto;
     2) debounce: N mutaciones seguidas = 1 aviso;
     3) guarda de reentrada + `takeRecords()` despues de correr los consumidores,
        que DESCARTA las mutaciones que ellos mismos generaron (ej. mover un
        modal fuera del td). Asi no hace falta una pasada extra ni adivinar
        cuanto esperar.
   Los consumidores DEBEN ser idempotentes: se los puede llamar de mas.

   OJO: el debounce usa setTimeout y NO requestAnimationFrame. Medido: en un
   documento con `visibilityState === 'hidden'` (pestaña de fondo, iframe no
   pintado) el rAF NO se dispara nunca, y los consumidores no correrian hasta
   que la pestaña vuelva a estar visible. El listado tiene que quedar
   consistente aunque nadie lo este mirando.

   COMO AGREGAR UN CONSUMIDOR NUEVO: window.SirioList.onChange(fn). No hace
   falta tocar `replaceListFromHtml` ni este bloque.                            */
(function(){
  var consumidores = [];
  var observadores = [];
  var observados = (typeof WeakSet === 'function') ? new WeakSet() : null;
  var corriendo = false;
  var agendado = false;

  function notificar(){
    if (corriendo) return;
    corriendo = true;
    try {
      for (var i = 0; i < consumidores.length; i++){
        // un consumidor que explota no puede dejar sin correr a los que siguen
        try { consumidores[i](); } catch(e){ /* noop */ }
      }
    } finally {
      // se descartan las mutaciones que generaron los propios consumidores,
      // para que no se rebote una segunda pasada inutil
      for (var j = 0; j < observadores.length; j++){
        try { observadores[j].takeRecords(); } catch(e){ /* noop */ }
      }
      corriendo = false;
    }
  }

  function agendar(){
    if (agendado) return;
    agendado = true;
    setTimeout(function(){
      agendado = false;
      notificar();
    }, 0);
  }

  function observar(tbody){
    if (!tbody) return;
    if (observados){
      if (observados.has(tbody)) return;
      observados.add(tbody);
    } else {
      if (tbody.__sirioObservado) return;
      tbody.__sirioObservado = true;
    }
    try {
      var mo = new MutationObserver(agendar);
      mo.observe(tbody, { childList: true, subtree: true });
      observadores.push(mo);
    } catch(e){ /* navegador sin MutationObserver: queda el aviso inicial */ }
  }

  function observarTodo(){
    try {
      document.querySelectorAll('table tbody').forEach(observar);
    } catch(e){ /* noop */ }
  }

  window.SirioList = {
    // registra un consumidor. Se lo llama en el arranque y en cada cambio.
    onChange: function(fn){ if (typeof fn === 'function') consumidores.push(fn); },
    // fuerza un aviso (para mutaciones que no pasan por el <tbody>)
    refresh: notificar,
    // permite observar un <tbody> que aparecio despues del arranque
    observe: observar
  };

  function init(){ observarTodo(); notificar(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

(function(){
  // Los menus de acciones de las tablas viven dentro de .table-responsive, que por el
  // overflow-x de Bootstrap clippea tambien en vertical: por spec CSS, overflow-y:visible
  // se computa a auto cuando el otro eje no es visible. Posicionar el menu contra el
  // viewport lo saca de ese recorte sin tocar el scroll horizontal de la tabla.
  //
  // Va colgado de SirioList y NO de DOMContentLoaded: los toggles se destruyen
  // en cada repintado AJAX del listado y hay que volver a fijarlos.
  //
  /* ─────────────────────────────────────────────────────────────────────────
     POR QUE NO ALCANZA CON `getOrCreateInstance(t, config)` A SECAS
     `getOrCreateInstance` DEVUELVE LA INSTANCIA EXISTENTE E IGNORA EL CONFIG
     (bootstrap 5.3: `getInstance(el) || new this(el, config)`). O sea: el
     popperConfig se aplica SOLO si somos los primeros en crear la instancia.
     Y no siempre lo somos:
       - el data-api de bootstrap hace `Dropdown.getOrCreateInstance(this).toggle()`
         en el click, SIN config;
       - `safeHideDropdown()` de empresas.html / clientes.html hacia lo mismo.
     Tras `tbody.innerHTML = ...` los toggles son nodos NUEVOS sin instancia, y
     nosotros los arreglamos recien un macrotask despues (el `setTimeout(0)` de
     `agendar()`). Si un tap cae en esa ventana — el dedo ya venia bajando
     cuando llego la respuesta del fetch — bootstrap crea la instancia sin
     popperConfig y a partir de ahi NO HAY FORMA de reconfigurarla: el menu
     queda `position:absolute` y lo clippea el `.table-responsive`. Con la tabla
     filtrada a una o dos filas el contenedor es bajito y el menu se ve cortado.

     Por eso, si encontramos una instancia que no creamos nosotros, se la
     descarta y se crea de nuevo con el config. La marca en el elemento evita
     destruir y recrear en cada aviso de SirioList.                           */
  const MARCA_FIJADO = '_sirioMenuFijado';
  function fijarMenusDeTabla(){
    if(!(window.bootstrap && window.bootstrap.Dropdown)) return;
    const Dropdown = window.bootstrap.Dropdown;
    const opciones = { popperConfig: base => Object.assign({}, base, { strategy: 'fixed' }) };
    document.querySelectorAll('.table-responsive [data-bs-toggle="dropdown"]').forEach(t => {
      try {
        if (t[MARCA_FIJADO] && Dropdown.getInstance(t)) return; // ya es nuestra y sigue viva
        const previa = Dropdown.getInstance(t);
        if (previa){
          // Menu abierto: descartarlo ahora lo dejaria pegado. Se reintenta en
          // el proximo aviso, cuando ya este cerrado.
          if (t.classList.contains('show')) return;
          try { previa.dispose(); } catch(e){ /* ya estaba muerta */ }
        }
        Dropdown.getOrCreateInstance(t, opciones);
        t[MARCA_FIJADO] = true;
      } catch(e){ /* un toggle roto no puede dejar sin arreglar a los demas */ }
    });
  }
  window.SirioList.onChange(fijarMenusDeTabla);
})();

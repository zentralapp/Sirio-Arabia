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
          // NO BORRAR el strip de modales. En /empresas y /clientes los modales
          // por fila (archiveCompany{id}, links{id}) viven DENTRO del <td> de
          // acciones. Clonar el innerHTML tal cual duplicaba esos id, y
          // `data-bs-target="#archiveCompany5"` resuelve SIEMPRE al primero del
          // documento: se abria el modal de la tabla escondida, no el de la card.
          // Los originales se suben a <body> en subirModalesDeFila().
          const clonAcciones = lastTd.cloneNode(true);
          clonAcciones.querySelectorAll('.modal').forEach(m => m.remove());
          actionsHTML = clonAcciones.innerHTML;
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

  // Sube a <body> los modales que viven dentro del <td> de acciones de cada
  // fila. Hace falta porque en modo cards el `.desktop-table` esta en
  // display:none, y un modal con un ancestro display:none NO se puede mostrar
  // por mas que sea position:fixed. Subirlos los deja alcanzables por id desde
  // los botones de la card.
  // Es idempotente a proposito: el hook lo puede llamar de mas, y en cada
  // repintado AJAX aparecen modales nuevos en el tbody que reemplazan a los ya
  // subidos (si no, quedarian ids duplicados otra vez).
  function subirModalesDeFila(wrapper){
    try{
      wrapper.querySelectorAll('.modal[id]').forEach(function(m){
        if(m.classList.contains('show')) return; // no mover uno abierto
        // OJO: aca NO sirve document.getElementById(m.id). Devuelve el PRIMERO en
        // orden de documento, que es el modal nuevo que todavia esta en el tbody,
        // no la copia vieja que quedo colgada de <body> en la pasada anterior.
        // Resultado medido con ese error: despues de buscar quedaban los 10 ids
        // duplicados. Hay que buscar explicitamente entre los hijos de <body>.
        var id = m.id;
        var hijos = document.body.children;
        for(var i = hijos.length - 1; i >= 0; i--){
          var p = hijos[i];
          if(p !== m && p.id === id && p.classList && p.classList.contains('modal')
             && !p.classList.contains('show')) p.remove();
        }
        document.body.appendChild(m);
      });
    } catch(e){ /* noop */ }
  }

  function rebuild(){
    const wrappers = document.querySelectorAll('.desktop-table');
    wrappers.forEach(w => {
      let cards = w.nextElementSibling;
      if(!cards || !cards.classList.contains('mobile-cards')){
        cards = document.createElement('div');
        cards.className = 'mobile-cards';
        w.parentNode.insertBefore(cards, w.nextSibling);
      }
      // El CSS es la UNICA fuente de verdad del breakpoint. Antes esto lo
      // decidia `isMobilePortrait()` (<768 y portrait) en paralelo a la hoja de
      // estilos, y las dos definiciones podian divergir: hoy /clientes y
      // /empresas pasan a cards hasta 991.98px en cualquier orientacion, asi que
      // preguntarle al computed style evita duplicar el corte en dos lugares.
      const enModoCards = window.getComputedStyle(cards).display !== 'none';
      if(enModoCards){ subirModalesDeFila(w); buildCardsFor(w); }
      else if(cards.childElementCount) cards.innerHTML = '';
    });
  }

  // Colgado de SirioList: las cards tienen que rehacerse despues de cada
  // repintado AJAX del listado, igual que los dropdowns. onChange() ya dispara
  // una vez en el arranque, asi que no hace falta DOMContentLoaded aparte.
  window.SirioList.onChange(rebuild);
  // el resize/rotacion no muta el tbody, asi que el observer no lo ve
  window.addEventListener('resize', function(){ clearTimeout(window.__mc_to); window.__mc_to = setTimeout(rebuild, 150); });
})();

(function(){
  // Los menus de acciones de las tablas viven dentro de .table-responsive, que por el
  // overflow-x de Bootstrap clippea tambien en vertical: por spec CSS, overflow-y:visible
  // se computa a auto cuando el otro eje no es visible. Posicionar el menu contra el
  // viewport lo saca de ese recorte sin tocar el scroll horizontal de la tabla.
  //
  // Va colgado de SirioList y NO de DOMContentLoaded: los toggles se destruyen
  // en cada repintado AJAX del listado y hay que volver a fijarlos.
  function fijarMenusDeTabla(){
    if(!(window.bootstrap && window.bootstrap.Dropdown)) return;
    const toggles = document.querySelectorAll('.table-responsive [data-bs-toggle="dropdown"]');
    toggles.forEach(t => {
      window.bootstrap.Dropdown.getOrCreateInstance(t, {
        popperConfig: base => Object.assign({}, base, { strategy: 'fixed' })
      });
    });
  }
  window.SirioList.onChange(fijarMenusDeTabla);
})();

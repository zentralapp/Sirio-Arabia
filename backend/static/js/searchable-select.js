/*
 * Campo con busqueda + dropdown.
 *
 * Monta Tom Select sobre los <select> que ya existen, sin tocar el markup ni
 * el JS de cada vista. Opt-in declarativo: alcanza con ponerle el atributo
 * data-searchable al <select>.
 *
 *     <select class="form-select" id="pedido_cliente" data-searchable>
 *
 * Decisiones y trampas, para que no las repita el que venga:
 *
 * 1. PROGRESSIVE ENHANCEMENT. Se monta sobre las <option> que ya renderiza
 *    Jinja. Si el CDN no carga, queda el <select> nativo funcionando: el form
 *    se sigue enviando igual. Por eso no hay endpoint remoto ni plantilla JS.
 *
 * 2. FILTRADO DEL LADO DEL CLIENTE. Hay ~10 clientes y ~5 empresas. Traer eso
 *    por AJAX seria peor: mas latencia y mas codigo para el mismo resultado.
 *
 * 3. ORDEN DE SCRIPTS. En layout.html el bloque `content` esta ANTES de los
 *    <script> del pie, asi que TODO script inline de una vista corre antes de
 *    que exista window.bootstrap. Por eso la libreria se carga en el <head>
 *    (mismo criterio que fechas.js) y este archivo inicializa dentro de
 *    DOMContentLoaded: cuando corre, el DOM y la libreria ya existen.
 *
 * 4. LOS data-* DE LAS <option> SON SAGRADOS. pedidos.html lee data-mail,
 *    data-plazo, data-tipo-default, data-nota-pedido, data-razon,
 *    data-forma-pago, etc. desde la option seleccionada. Tom Select conserva
 *    los nodos <option> originales (guarda la referencia en option.$option),
 *    asi que los data-* sobreviven. Si se cambia de libreria, ESTO HAY QUE
 *    VOLVER A VERIFICAR o se rompe el autocompletado de pedidos en silencio.
 *
 * 5. REPOBLADO DINAMICO. #pedido_empresa y #companySelect se llenan por JS
 *    despues de elegir cliente (fetch a /api/clientes/<id>/empresas). Tom
 *    Select 2.x no tiene un sync() que relea el <select>, asi que se observa
 *    el <select> con un MutationObserver y se reinicializa cuando cambia el
 *    CONJUNTO de opciones. Se compara una firma ordenada de las opciones, no
 *    el orden: Tom Select reordena las <option> al seleccionar, y si
 *    reaccionaramos a eso nos reinicializariamos solos en loop.
 *
 * 6. ACENTOS. Tom Select filtra ignorando diacriticos (opcion `diacritics`,
 *    true por defecto). Se deja explicito igual, para que quede consistente
 *    con la busqueda del backend, que tambien ignora acentos.
 */
(function () {
  'use strict';

  var ATTR = 'data-searchable';

  // childList: para detectar repoblado por JS.
  // attributes: para seguir los cambios de visibilidad/habilitado que hace la vista.
  var OBSERVE_OPTS = { childList: true, attributes: true, attributeFilter: ['class', 'disabled'] };

  function optionsSignature(select) {
    // Ordenada a proposito: nos interesa QUE opciones hay, no en que orden.
    // Tom Select mueve la <option> seleccionada al final cuando cambia el
    // valor; eso no es un repoblado y no debe disparar reinicializacion.
    return Array.prototype.map
      .call(select.options, function (o) {
        return o.value + '' + o.textContent;
      })
      .sort()
      .join('');
  }

  function placeholderFor(select) {
    // La primera option vacia ("Cliente...", "Seleccionar...") es el
    // placeholder de toda la vida. Se reusa como placeholder del buscador.
    var first = select.options[0];
    if (first && first.value === '') return first.textContent.trim();
    return 'Buscar...';
  }

  function build(select) {
    return new window.TomSelect(select, {
      create: false,
      allowEmptyOption: true,
      diacritics: true,
      maxOptions: null,
      placeholder: placeholderFor(select),
      // Busca tanto por el texto visible como por el valor.
      searchField: ['text'],
      // Sin esto, al reabrir el dropdown queda filtrado por lo ultimo tipeado.
      onDropdownOpen: function () {
        this.clearFilter && this.clearFilter();
      }
    });
  }

  function mount(select) {
    if (select.tomselect) return;

    var instance;
    try {
      instance = build(select);
    } catch (err) {
      // Si falla, el <select> nativo queda intacto y usable.
      if (window.console) console.error('[searchable-select] no se pudo montar', select.id || select.name, err);
      return;
    }

    var signature = optionsSignature(select);
    var rebuilding = false;

    // Tom Select esconde el <select> original y muestra su propio wrapper. Todo
    // lo que la vista le haga al <select> para mostrarlo/ocultarlo/deshabilitarlo
    // hay que reflejarlo en el wrapper, o el control queda visible cuando no
    // corresponde. index.html hace exactamente eso: toggleScopeFilter() prende y
    // apaga .d-none y .disabled sobre #dashboardCompany / #dashboardClient.
    function mirrorState() {
      var ts = select.tomselect;
      if (!ts || !ts.wrapper) return;

      ts.wrapper.classList.toggle('d-none', select.classList.contains('d-none'));

      // enable()/disable() son idempotentes aca: solo se llaman si el estado
      // difiere, asi que no se retroalimentan con el observer.
      if (select.disabled && !ts.isDisabled) ts.disable();
      else if (!select.disabled && ts.isDisabled) ts.enable();
    }

    var observer = new MutationObserver(function (mutations) {
      if (rebuilding) return;

      if (mutations.some(function (m) { return m.type === 'attributes'; })) {
        mirrorState();
      }

      var next = optionsSignature(select);
      if (next === signature) return; // solo reordenamiento propio de Tom Select

      rebuilding = true;
      observer.disconnect();

      var previousValue = select.value;
      // TRAMPA: destroy() hace `input.innerHTML = revertSettings.innerHTML`, o sea
      // restaura las <option> que habia AL MONTAR y se lleva puestas las que la
      // vista acaba de traer por fetch. Por eso guardamos el markup real antes de
      // destruir y lo volvemos a poner despues. Guardar el HTML (y no los nodos)
      // conserva los data-* que lee pedidos.html.
      var liveMarkup = select.innerHTML;

      try {
        select.tomselect.destroy();
        select.innerHTML = liveMarkup;
        instance = build(select);
        // Si el valor sigue siendo una opcion valida, se respeta.
        if (previousValue && select.querySelector('option[value="' + window.CSS.escape(previousValue) + '"]')) {
          instance.setValue(previousValue, true);
        }
      } catch (err) {
        if (window.console) console.error('[searchable-select] fallo el resync', select.id || select.name, err);
      }

      signature = optionsSignature(select);
      mirrorState();
      observer.observe(select, OBSERVE_OPTS);
      rebuilding = false;
    });

    // Estado inicial: el <select> puede venir ya oculto/deshabilitado desde Jinja
    // (es el caso de #dashboardClient), asi que hay que reflejarlo al montar.
    mirrorState();
    observer.observe(select, OBSERVE_OPTS);
  }

  function init() {
    if (!window.TomSelect) {
      // El CDN no cargo. No es fatal: quedan los <select> nativos.
      if (window.console) console.warn('[searchable-select] TomSelect no disponible, se usan los select nativos');
      return;
    }
    document.querySelectorAll('select[' + ATTR + ']').forEach(mount);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Para selects que se agregan al DOM despues (modales renderizados por JS).
  window.SirioSearchableSelect = { mount: mount, initAll: init };
})();

/* ==========================================================================
 * SirioFechas -- FUENTE UNICA DE VERDAD de fechas en el frontend.
 *
 * Antes habia 9 rutinas de fecha distintas repartidas en los templates
 * (parseIsoDate, 2x parseDateValue, fmtIsoToAr, formatDate, split('-')
 * .reverse().join('/'), 2x isoToday, ...). Cada una parseaba distinto y dos
 * de ellas se comian un dia por el problema de UTC descrito abajo.
 *
 * --------------------------------------------------------------------------
 * CONVENCION DE HUSO (importante, es la razon de existir de este archivo)
 * --------------------------------------------------------------------------
 * new Date('2026-09-10')            -> se interpreta como UTC medianoche.
 * new Date('2026-09-10T00:00:00')   -> se interpreta como hora LOCAL.
 *
 * En Argentina (UTC-3) la primera forma cae el 2026-09-09 a las 21:00 LOCAL.
 * Si despues se compara contra un "hoy" local (new Date() + setHours(0,0,0,0))
 * la fecha aparece corrida un dia y una cobranza se marca ATRASADA 24hs antes.
 *
 * CONVENCION ELEGIDA: una fecha SIN hora es una fecha de NEGOCIO, no un
 * instante. Se construye SIEMPRE con new Date(anio, mes, dia), que es
 * medianoche LOCAL. Nunca se usa new Date(string) ni Date.parse(string) para
 * una fecha sin hora. Un string CON hora si se delega a Date.parse (ahi el
 * huso viene explicito o es intencionalmente local).
 *
 * Para "hoy" NO se usa el reloj del navegador cuando hay alternativa: la
 * Fase 4 expone el hoy del negocio (hora de Buenos Aires, calculado por
 * backend/models.py -> hoy_negocio()). Pasalo por SirioFechas.setHoyNegocio().
 * ========================================================================== */
(function (global) {
  'use strict';

  var FMT_VISIBLE = 'DD/MM/AAAA';

  // Fecha sin hora: AAAA-MM-DD o AAAA-M-D
  var RE_ISO = /^(\d{4})-(\d{1,2})-(\d{1,2})$/;
  // Fecha sin hora: DD/MM/AAAA, D/M/AAAA, DD-MM-AAAA, D-M-AAAA
  var RE_DMY = /^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/;
  // 8 digitos pegados. Se prueba AAAAMMDD antes que DDMMAAAA para respetar la
  // MISMA prioridad que el backend (_parse_datetime_like prueba
  // datetime.fromisoformat primero, y esa acepta "20260910" como ISO).
  var RE_YYYYMMDD = /^(\d{4})(\d{2})(\d{2})$/;
  var RE_DDMMYYYY = /^(\d{2})(\d{2})(\d{4})$/;
  // Fecha CON hora (ISO): se delega a Date.parse
  var RE_ISO_DT = /^\d{4}-\d{1,2}-\d{1,2}[T ]\d/;

  var hoyNegocioIso = '';

  function pad2(n) {
    return (n < 10 ? '0' : '') + n;
  }

  /* Construye una Date a medianoche LOCAL y valida que no haya rebalseado
   * (new Date(2026, 1, 31) no explota: te da el 3 de marzo). */
  function localDate(y, m, d) {
    if (!(y >= 1 && m >= 1 && m <= 12 && d >= 1 && d <= 31)) return null;
    var dt = new Date(y, m - 1, d);
    if (isNaN(dt.getTime())) return null;
    if (dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== d) return null;
    return dt;
  }

  /* parse(v) -> Date (medianoche local si v no trae hora) | null
   * Acepta los MISMOS formatos que el parser del backend
   * (_parse_datetime_like en backend/views/main.py). */
  function parse(v) {
    if (v == null) return null;
    if (v instanceof Date) return isNaN(v.getTime()) ? null : v;

    var s = String(v).trim();
    if (!s) return null;

    var m = s.match(RE_ISO);
    if (m) return localDate(+m[1], +m[2], +m[3]);

    m = s.match(RE_DMY);
    if (m) return localDate(+m[3], +m[2], +m[1]);

    m = s.match(RE_YYYYMMDD);
    if (m) {
      var iso8 = localDate(+m[1], +m[2], +m[3]);
      if (iso8) return iso8;
    }
    m = s.match(RE_DDMMYYYY);
    if (m) return localDate(+m[3], +m[2], +m[1]);

    // Con hora explicita: ahi si Date.parse es correcto.
    if (RE_ISO_DT.test(s)) {
      var t = Date.parse(s);
      return isNaN(t) ? null : new Date(t);
    }
    return null;
  }

  /* time(v) -> milisegundos | null. Para ordenar y comparar. */
  function time(v) {
    var d = parse(v);
    return d ? d.getTime() : null;
  }

  /* format(v) -> 'DD/MM/AAAA' | fallback. Fuente unica de la fecha visible. */
  function format(v, fallback) {
    var d = parse(v);
    if (!d) return fallback == null ? '' : fallback;
    return pad2(d.getDate()) + '/' + pad2(d.getMonth() + 1) + '/' + d.getFullYear();
  }

  /* toIso(v) -> 'AAAA-MM-DD' | fallback. Es lo que espera el backend y el
   * dateFormat 'Y-m-d' de flatpickr. NO es formato de lectura.
   * Se construye con los getters LOCALES a proposito. toISOString() pasa por
   * UTC: con offset negativo (Argentina, UTC-3) da el mismo dia, pero con
   * offset POSITIVO devuelve el dia anterior. Asi no depende del huso. */
  function toIso(v, fallback) {
    var d = parse(v);
    if (!d) return fallback == null ? '' : fallback;
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  /* --- "hoy" -------------------------------------------------------------
   * setHoyNegocio(iso) lo llama cada pagina que recibe el hoy del negocio
   * desde el backend (Fase 4). Si nadie lo setea se cae al reloj del
   * navegador, que es lo que habia antes. */
  function setHoyNegocio(iso) {
    var d = parse(iso);
    hoyNegocioIso = d ? toIso(d) : '';
    return hoyNegocioIso;
  }

  function hoyIso() {
    if (hoyNegocioIso) return hoyNegocioIso;
    return toIso(new Date());
  }

  /* Devuelve SIEMPRE una Date nueva a medianoche local: los callers le hacen
   * setDate()/getTime(), asi que no se puede compartir una sola instancia. */
  function hoy() {
    var d = parse(hoyIso());
    if (!d) {
      d = new Date();
      d.setHours(0, 0, 0, 0);
    }
    return d;
  }

  /* addDays(v, n) -> Date nueva. Sin aritmetica de milisegundos, asi el
   * cambio de horario de verano no mueve la fecha. */
  function addDays(v, n) {
    var d = parse(v);
    if (!d) return null;
    var out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    out.setDate(out.getDate() + (parseInt(n, 10) || 0));
    return out;
  }

  global.SirioFechas = {
    FMT_VISIBLE: FMT_VISIBLE,
    parse: parse,
    time: time,
    format: format,
    toIso: toIso,
    setHoyNegocio: setHoyNegocio,
    hoyIso: hoyIso,
    hoy: hoy,
    addDays: addDays
  };
})(window);

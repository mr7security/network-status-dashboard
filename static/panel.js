/* panel.js — Monitor de red. Sin frameworks ni CDN.
   Habla con /api/v1/ y pinta dos vistas: seguimiento y Modo TV. */

"use strict";

const REFRESCO_S      = 15;   // cada cuanto pedimos el estado
const SERIE_REFRESCO_S = 300; // cada cuanto refrescamos las mini-graficas
const MAX_TV          = 5;    // filas de problemas por tarjeta en Modo TV
const MAX_CSV_MB      = 24;   // el servidor corta en 25 contando la envoltura

let series = new Map();       // grupo -> puntos de las ultimas 24 h
let modoTV = false;
let permisos = {subida_csv: true, mantenimiento: true};
const abiertos = new Set();   // bloques desplegados, para no cerrarlos al refrescar
let ultimosGrupos = [];       // nombres vistos en el ultimo pintado

/* ------------------------------------------------------------ utilidades */

function esc(t){
  return String(t == null ? "" : t)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function hace(ts){
  if(!ts) return "";
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if(s < 60)    return s + "s";
  if(s < 3600)  return Math.floor(s/60) + "m";
  if(s < 86400) return Math.floor(s/3600) + "h";
  return Math.floor(s/86400) + "d";
}

function cuando(ts){
  if(!ts) return "";
  const d = new Date(ts*1000);
  const hoy = new Date().toDateString() === d.toDateString();
  return (hoy ? "hoy a las " : d.toLocaleDateString("es-ES") + " ")
       + d.toLocaleTimeString("es-ES", {hour:"2-digit", minute:"2-digit"});
}

const ICONOS = Object.assign(Object.create(null), {
  camaras:"◉", wifi:"≋", switches:"⇅", red:"⌂", wan:"⇄", servidores:"▤",
  puestos:"▭", impresoras:"⎙", fabrica:"⚙", basculas:"⚖", pantallas:"□",
  voip:"✆", externos:"↗", radioenlaces:"📡", virtualizacion:"❏",
  pruebas:"⚗", otros:"·"
});
const icono = n => ICONOS[n] || "·";

let temporizadorBanner = null;

function banner(id, html, malo, segundos){
  const caja = document.getElementById(id);
  if(!html){ caja.style.display = "none"; return; }
  caja.innerHTML = html;
  caja.className = "banner" + (malo ? " malo" : "");
  caja.style.display = "block";
  if(id === "aviso-accion"){
    clearTimeout(temporizadorBanner);
    if(segundos){
      temporizadorBanner = setTimeout(()=>{ caja.style.display = "none"; },
                                      segundos*1000);
    }
  }
}

function relojear(){
  document.getElementById("reloj").textContent =
    new Date().toLocaleTimeString("es-ES");
}
setInterval(relojear, 1000); relojear();

/* --------------------------------------------------------------- sparkline */

function pintarSpark(lienzo, grupo){
  const ctx = lienzo.getContext("2d");
  const an = lienzo.clientWidth || 190, al = 34;
  lienzo.width = an * devicePixelRatio;
  lienzo.height = al * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.clearRect(0, 0, an, al);

  const puntos = (series.get(grupo) || []).map(p=>{
    const total = p.ok + p.aviso + p.caido;
    return total ? 100*p.ok/total : 100;
  });
  if(puntos.length < 2) return;

  const minimo = Math.min(...puntos, 99);
  const rango = Math.max(2, 100 - minimo);
  ctx.beginPath();
  puntos.forEach((v,i)=>{
    const x = i*(an/(puntos.length-1));
    const y = al - ((v - (100-rango))/rango)*(al-4) - 2;
    i ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
  });
  ctx.strokeStyle = "#38bdf8"; ctx.lineWidth = 1.4; ctx.stroke();
  ctx.lineTo(an, al); ctx.lineTo(0, al); ctx.closePath();
  ctx.fillStyle = "rgba(56,189,248,.13)"; ctx.fill();
}

/* ---------------------------------------------------------- mantenimiento */

const DURACIONES = [["8 horas",8], ["24 horas",24], ["7 días",168],
                    ["Indefinido",0]];

function botonMant(p, enMant){
  if(!permisos.mantenimiento) return "";
  return enMant
    ? `<button class="btn-mant" data-ip="${esc(p.ip)}" data-quitar="1"
         title="Quitar la marca de mantenimiento">✕</button>`
    : `<button class="btn-mant" data-ip="${esc(p.ip)}"
         title="Marcar en mantenimiento">🔧</button>`;
}

function abrirMenuMant(boton){
  const menu = document.getElementById("menu-mant");
  const ip = boton.dataset.ip;
  menu.innerHTML = `<div class="tit">${esc(ip)} · en mantenimiento durante</div>`
    + DURACIONES.map(([txt,h])=>`<button data-horas="${h}">${txt}</button>`).join("");
  const r = boton.getBoundingClientRect();
  menu.style.display = "block";
  menu.style.top  = (window.scrollY + r.bottom + 4) + "px";
  menu.style.left = (window.scrollX + Math.min(r.left, innerWidth - 200)) + "px";
  menu.querySelectorAll("button").forEach(b=>{
    b.addEventListener("click", ()=>{
      menu.style.display = "none";
      marcarMantenimiento(ip, Number(b.dataset.horas));
    });
  });
}

async function marcarMantenimiento(ip, horas){
  try{
    const resp = await fetch("/api/v1/mantenimiento", {
      method:"POST",
      headers:{"Content-Type":"application/json", "X-Monitor":"1"},
      body: JSON.stringify({ip, horas})
    });
    const datos = await resp.json();
    if(!resp.ok || datos.error) throw new Error(datos.error || ("HTTP "+resp.status));
    cargarEstado();
  }catch(e){
    banner("aviso-accion", `No se ha podido cambiar el mantenimiento de `
      + `<b>${esc(ip)}</b>: ${esc(e.message)}`, true, 20);
  }
}

document.addEventListener("click", ev=>{
  const menu = document.getElementById("menu-mant");
  const boton = ev.target.closest(".btn-mant");
  if(boton){
    ev.preventDefault();
    if(boton.dataset.quitar) marcarMantenimiento(boton.dataset.ip, -1);
    else abrirMenuMant(boton);
    return;
  }
  if(!ev.target.closest("#menu-mant")) menu.style.display = "none";
});

/* ------------------------------------------------------------ subida CSV */

async function subirCSV(fichero, forzar){
  if(!fichero) return;
  if(!/\.csv$/i.test(fichero.name)){
    return banner("aviso-accion", `El fichero debe ser un <b>.csv</b> exportado `
      + `del Dude (has elegido <b>${esc(fichero.name)}</b>).`, true, 30);
  }
  if(fichero.size > MAX_CSV_MB*1024*1024){
    return banner("aviso-accion",
      `El fichero ocupa demasiado (máximo ${MAX_CSV_MB} MB).`, true, 30);
  }
  if(fichero.size < 200){
    return banner("aviso-accion", "El fichero está prácticamente vacío.", true, 30);
  }

  const boton = document.getElementById("btn-csv");
  boton.disabled = true;
  banner("aviso-accion", `Procesando <b>${esc(fichero.name)}</b>…`, false, 180);
  const cuerpo = new FormData();
  cuerpo.append("csv", fichero, fichero.name);
  try{
    const resp = await fetch("/api/v1/inventario" + (forzar ? "?forzar=1" : ""), {
      method:"POST", headers:{"X-Monitor":"1"}, body:cuerpo
    });
    const datos = await resp.json();
    if(resp.status === 409 && datos.confirmar){
      if(confirm(datos.error + "\n\n¿Seguro que quieres aplicarlo?")){
        return await subirCSV(fichero, true);
      }
      return banner("aviso-accion",
        "Actualización cancelada. El inventario no se ha tocado.", false, 20);
    }
    if(!resp.ok || datos.error) throw new Error(datos.error || ("HTTP "+resp.status));
    const desc = Object.entries(datos.descartes || {})
      .map(([k,v])=>`${Number(v)} ${esc(k)}`).join(", ");
    banner("aviso-accion", `✓ Inventario actualizado: <b>${Number(datos.equipos)}</b> `
      + `equipos (${Number(datos.altas)} altas, ${Number(datos.bajas)} bajas)`
      + (desc ? ` · descartados: ${desc}` : ""), false, 45);
    setTimeout(cargarEstado, 4000);
  }catch(e){
    banner("aviso-accion",
      `No se ha podido actualizar el inventario: ${esc(e.message)}`, true, 60);
  }finally{
    boton.disabled = false;
  }
}

/* ---------------------------------------------------------------- tarjetas */

function claseNivel(g){
  return g.nivel === "error" ? "error" : g.nivel === "aviso" ? "aviso" : "ok";
}

function barra(g){
  const t = g.total || 1;
  return `<div class="barra">`
    + `<i class="b-ok" style="width:${100*g.ok/t}%"></i>`
    + `<i class="b-aviso" style="width:${100*g.aviso/t}%"></i>`
    + `<i class="b-error" style="width:${100*g.caido/t}%"></i>`
    + `<i class="b-mant" style="width:${100*(g.mantenimiento||0)/t}%"></i></div>`;
}

function tarjetaCompacta(div, g){
  div.className = "grupo-tarjeta compacta " + claseNivel(g);
  div.innerHTML = `<i class="led"></i><b>${esc(g.nombre)}</b>`
    + `<span class="txt">${g.total} equipos · todos responden</span>`
    + `<span class="pct">${g.porcentaje.toFixed(0)}%</span>`;
}

function tarjetaTV(div, g){
  div.className = "grupo-tarjeta " + g.nivel;
  const lista = (g.problemas || []).filter(p=>p.nivel !== "mantenimiento");
  const filas = !lista.length
    ? `<div class="todo-ok">✓ Todos los equipos responden</div>`
    : `<div class="lista-tv">` + lista.slice(0, MAX_TV).map(p=>`
        <div class="${p.nivel==='aviso'?'aviso':''} ${p.critico?'critico':''}">
          <i></i><span class="nom">${p.critico?"★ ":""}${esc(p.nombre)}</span>
          <span class="desde">${hace(p.desde)}</span>
        </div>`).join("") + `</div>`;
  const pendientes = Math.max(0, lista.length - MAX_TV);

  div.innerHTML = `
    <div class="cab">
      <h2><i class="led"></i>${esc(g.nombre)}<small>${g.total} equipos</small></h2>
      <div class="medida">
        <div class="grande ${claseNivel(g)}">${g.porcentaje.toFixed(0)}<span>%</span></div>
      </div>
    </div>
    ${barra(g)}
    ${filas}
    ${pendientes ? `<div class="mas">y ${pendientes} más…</div>` : ""}`;
}

function tarjetaDetalle(div, g){
  div.className = "grupo-tarjeta " + g.nivel;

  const fallos = Object.entries(g.desglose || {})
    .map(([tipo,v])=>[tipo, v.caido||0, v.aviso||0])
    .filter(f=>f[1]+f[2] > 0)
    .sort((a,b)=>(b[1]+b[2])-(a[1]+a[2]))
    .map(([tipo,caido,aviso])=>
      `<span class="etq mal">${icono(tipo)} ${esc(tipo)}`
      + (caido ? ` <b>${caido}</b>` : "")
      + (aviso ? ` <b style="color:var(--aviso)">${aviso}</b>` : "")
      + `</span>`).join("");

  // agrupamos por tipo; el mantenimiento va a su propio bloque, al final
  const cubos = new Map();
  for(const p of (g.problemas || [])){
    const clave = p.nivel === "mantenimiento" ? "mantenimiento" : p.tipo;
    if(!cubos.has(clave)) cubos.set(clave, []);
    cubos.get(clave).push(p);
  }
  const ordenados = [...cubos.entries()].sort((a,b)=>{
    if((a[0]==="mantenimiento") !== (b[0]==="mantenimiento"))
      return a[0]==="mantenimiento" ? 1 : -1;
    const cA = a[1].some(p=>p.critico), cB = b[1].some(p=>p.critico);
    if(cA !== cB) return cA ? -1 : 1;
    return b[1].length - a[1].length;
  });

  const bloques = ordenados.map(([tipo, lista])=>{
    const esMant = tipo === "mantenimiento";
    const clave = g.nombre + "|" + tipo;
    const caidos = lista.filter(p=>p.nivel === "caido").length;
    const avisos = lista.filter(p=>p.nivel === "aviso").length;
    const criticos = esMant ? 0 : lista.filter(p=>p.critico).length;

    const filas = lista.map(p=>{
      const mant = p.nivel === "mantenimiento";
      const detalle = mant
        ? (p.hasta ? "en mantenimiento · vuelve " + cuando(p.hasta)
                   : "en mantenimiento")
        : (p.detalle || (p.nivel === "aviso" ? "servicio caído"
                                             : "no responde"));
      return `
      <tr class="${p.nivel} ${p.critico?'critico':''}">
        <td class="t-nom" title="${esc(p.nombre)}">${p.critico?"★ ":""}${esc(p.nombre)}</td>
        <td class="t-ip">${esc(p.ip)}</td>
        <td>${icono(p.tipo)} ${esc(p.tipo)}</td>
        <td class="t-detalle" title="${esc(detalle)}">${esc(detalle)}</td>
        <td class="t-desde">${hace(p.desde)}</td>
        <td class="t-acc">${botonMant(p, mant)}</td>
      </tr>`;
    }).join("");

    return `<details class="bloque" data-clave="${esc(clave)}"
              ${abiertos.has(clave) ? "open" : ""}>
      <summary>
        <span>${esMant ? "🔧 en mantenimiento"
                       : icono(tipo) + " " + esc(tipo)}</span>
        <span class="n">
          ${criticos ? `<span class="pastilla crit">★ ${criticos} crítico${criticos>1?"s":""}</span>` : ""}
          ${caidos ? `<span class="pastilla error">${caidos} caído${caidos>1?"s":""}</span>` : ""}
          ${avisos ? `<span class="pastilla aviso">${avisos} con aviso</span>` : ""}
          ${esMant ? `<span class="pastilla mant">${lista.length} equipo${lista.length>1?"s":""}</span>` : ""}
        </span>
      </summary>
      <table class="equipos">
        <colgroup><col class="c-nom"><col class="c-ip"><col class="c-tipo">
          <col class="c-detalle"><col class="c-desde"><col class="c-acc"></colgroup>
        <tr><th>Equipo</th><th>Dirección</th><th>Tipo</th><th>Detalle</th>
            <th style="text-align:right">Desde hace</th><th></th></tr>
        ${filas}
      </table>
    </details>`;
  }).join("");

  const recorte = g.problemas_totales > (g.problemas || []).length
    ? `<div class="mas">Se muestran ${g.problemas.length} de `
      + `${g.problemas_totales} incidencias.</div>` : "";

  div.innerHTML = `
    <div class="cab">
      <h2><i class="led"></i>${esc(g.nombre)}<small>${g.total} equipos</small></h2>
      <div class="fallos">${fallos}</div>
      <div class="medida">
        <div class="grande ${claseNivel(g)}">${g.porcentaje.toFixed(0)}<span>%</span></div>
      </div>
      <canvas class="spark"></canvas>
    </div>
    ${barra(g)}
    <div class="resumen-linea">
      <span><b style="color:var(--ok)">${g.ok}</b> activos</span>
      <span><b style="color:var(--error)">${g.caido}</b> caídos</span>
      <span><b style="color:var(--aviso)">${g.aviso}</b> con aviso</span>
      ${g.mantenimiento ? `<span><b style="color:var(--mant)">${g.mantenimiento}</b> en mantenimiento</span>` : ""}
      ${g.criticos_caidos ? `<span><b style="color:var(--error)">${g.criticos_caidos}</b> críticos caídos</span>` : ""}
    </div>
    <div class="bloques">${bloques}${recorte}</div>`;

  div.querySelectorAll("details.bloque").forEach(d=>{
    d.addEventListener("toggle", ()=>{
      d.open ? abiertos.add(d.dataset.clave) : abiertos.delete(d.dataset.clave);
    });
  });
}

/* ----------------------------------------------------------------- pintado */

function pintar(estado){
  const r = estado.resumen || {};
  permisos = estado.permisos || permisos;

  document.getElementById("menu-mant").style.display = "none";
  document.getElementById("titulo").textContent = estado.titulo || "MONITOR DE RED";
  document.getElementById("btn-csv").style.display =
    permisos.subida_csv === false ? "none" : "";
  document.getElementById("c-total").textContent    = r.equipos ?? "–";
  document.getElementById("c-ok").textContent       = r.ok ?? "–";
  document.getElementById("c-aviso").textContent    = r.aviso ?? "–";
  document.getElementById("c-caido").textContent    = r.caido ?? "–";
  document.getElementById("c-mant").textContent     = r.mantenimiento ?? 0;
  document.getElementById("c-criticos").textContent = r.criticos_caidos ?? "–";

  const rejilla = document.getElementById("rejilla");
  rejilla.innerHTML = "";
  const nombres = (estado.grupos || []).map(g=>g.nombre);
  const cambio = nombres.join("|") !== ultimosGrupos.join("|");
  ultimosGrupos = nombres;
  if(cambio) cargarSeries();          // grupos nuevos: traemos sus series

  for(const g of (estado.grupos || [])){
    const div = document.createElement("div");
    const limpio = g.nivel === "ok" && !g.problemas_totales;
    if(modoTV)        tarjetaTV(div, g);
    else if(limpio)   tarjetaCompacta(div, g);
    else              tarjetaDetalle(div, g);
    rejilla.appendChild(div);
    const lienzo = div.querySelector("canvas.spark");
    if(lienzo) pintarSpark(lienzo, g.nombre);
  }

  const vigentes = new Set();
  rejilla.querySelectorAll("details.bloque")
         .forEach(d=>vigentes.add(d.dataset.clave));
  for(const c of [...abiertos]) if(!vigentes.has(c)) abiertos.delete(c);

  const gen = estado.generado
    ? new Date(estado.generado*1000).toLocaleTimeString("es-ES") : "–";
  document.getElementById("pie").textContent =
    `Último barrido ${gen} · ${r.duracion_s ?? "?"} s · ${r.metodo_ping || ""}`
    + (r.snmp ? " · SNMP disponible" : "")
    + ` · ${r.grupos_con_problemas || 0} de ${r.grupos || 0} grupos con incidencias`;
}

/* ------------------------------------------------------------------ datos */

async function cargarEstado(){
  try{
    const resp = await fetch("/api/v1/estado", {cache:"no-store"});
    if(!resp.ok) throw new Error("HTTP " + resp.status);
    const datos = await resp.json();
    banner("aviso-conexion", "");
    if(datos.generado) pintar(datos);
  }catch(e){
    banner("aviso-conexion",
      "Sin conexión con el servidor del monitor (" + esc(e.message) + ")", true);
  }
}

async function cargarSeries(){
  // una peticion por grupo visible: son unos pocos y solo cada 5 minutos
  const grupos = ultimosGrupos.slice(0, 30);
  await Promise.all(grupos.map(async nombre=>{
    try{
      const resp = await fetch("/api/v1/serie?horas=24&grupo="
                               + encodeURIComponent(nombre), {cache:"no-store"});
      if(resp.ok) series.set(nombre, (await resp.json()).puntos || []);
    }catch(e){ /* el historico es accesorio */ }
  }));
  for(const clave of [...series.keys()]){
    if(!grupos.includes(clave)) series.delete(clave);
  }
}

/* ---------------------------------------------------------------- eventos */

document.getElementById("btn-csv").addEventListener("click", ()=>{
  document.getElementById("fichero-csv").click();
});
document.getElementById("fichero-csv").addEventListener("change", ev=>{
  subirCSV(ev.target.files[0]);
  ev.target.value = "";
});
document.getElementById("btn-tv").addEventListener("click", ()=>{
  modoTV = !modoTV;
  document.body.classList.toggle("tv", modoTV);
  document.getElementById("btn-tv").textContent = modoTV ? "Vista normal" : "Modo TV";
  if(modoTV && document.documentElement.requestFullscreen){
    document.documentElement.requestFullscreen().catch(()=>{});
  }else if(!modoTV && document.fullscreenElement){
    document.exitFullscreen().catch(()=>{});
  }
  cargarEstado();
});
document.addEventListener("keydown", e=>{
  if((e.key === "t" || e.key === "T") && !e.ctrlKey && !e.altKey){
    document.getElementById("btn-tv").click();
  }
});

cargarEstado();
setInterval(cargarEstado, REFRESCO_S*1000);
setInterval(()=>cargarSeries().then(cargarEstado), SERIE_REFRESCO_S*1000);

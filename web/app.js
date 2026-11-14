const $=id=>document.getElementById(id),esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const LV={crit:['!','Rot','var(--crit)','Roter Alarm'],warn:['!','Gelb','var(--warn)','Gelber Alarm'],info:['i','Info','var(--info)','Hinweis'],ok:['✓','OK','var(--good)','Alle Systeme nominal']};
const num=(v,d=0)=>v==null?'–':v.toLocaleString('de-AT',{maximumFractionDigits:d,minimumFractionDigits:d});
const rate=v=>v==null?'–':v>=10?num(v,1)+' Mbit/s':v>=1?num(v,2)+' Mbit/s':num(v*1000)+' kbit/s';
const hm=t=>new Date(t).toLocaleTimeString('de-AT',{hour:'2-digit',minute:'2-digit'});
const hms=t=>new Date(t).toLocaleTimeString('de-AT');
const dur=s=>{const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?d+' T '+h+' h':h?h+' h '+m+' min':m+' min'};
const stardate=t=>{const d=new Date(t),y=d.getFullYear(),s=new Date(y,0,1),e=new Date(y+1,0,1);return num((y-2000)*1000+1000*(d-s)/(e-s),1)};

function lineChart(el,series,o){const W=Math.max(280,el.clientWidth||600),H=o.h||180,L=44,R=8,T=8,B=22,pw=W-L-R,ph=H-T-B;
 const t0=o.t0,t1=o.t1,all=series.flatMap(s=>s.pts.map(p=>p[1]));let max=o.max??Math.max(o.minMax||1,...all);
 if(o.max==null){const st=[0.025,0.05,0.1,0.25,0.5,1,2,5,10,20,25,50,100,200,500].find(x=>max/x<=4)||1000;max=4*st}
 const mn=o.min??0,X=t=>L+pw*(t-t0)/Math.max(1,t1-t0),Y=v=>T+ph*(1-(v-mn)/(max-mn));
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+esc(o.aria)+'">';
 for(let k=0;k<=4;k++){const v=mn+(max-mn)*k/4,y=Y(v);s+='<line class="'+(k?'gl':'ax')+'" x1="'+L+'" x2="'+(W-R)+'" y1="'+y+'" y2="'+y+'"/><text class="tl" x="'+(L-6)+'" y="'+(y+4)+'" text-anchor="end">'+o.fmtAxis(v)+'</text>'}
 if(o.thr!=null)s+='<line class="th" x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(o.thr)+'" y2="'+Y(o.thr)+'"/>';
 o.ticks.forEach((t,k)=>{const a=k==0?'start':k==o.ticks.length-1?'end':'middle';s+='<text class="tl" x="'+X(t)+'" y="'+(H-4)+'" text-anchor="'+a+'">'+hm(t)+'</text>'});
 for(const se of series){if(!se.pts.length)continue;s+='<polyline class="ln" stroke="'+se.color+'" points="'+se.pts.map(p=>X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1)).join(' ')+'"/>';
  if(se.pts.length<3)for(const p of se.pts)s+='<circle cx="'+X(p[0])+'" cy="'+Y(p[1])+'" r="4" fill="'+se.color+'" stroke="var(--bg)" stroke-width="2"/>'}
 s+='<line class="xh ax" y1="'+T+'" y2="'+(T+ph)+'" style="display:none;stroke:var(--o1)"/><rect x="'+L+'" y="'+T+'" width="'+pw+'" height="'+ph+'" fill="transparent"/></svg><div class="tip"></div>';
 el.innerHTML=s;const svg=el.querySelector('svg'),tip=el.querySelector('.tip'),xh=svg.querySelector('.xh');
 svg.onmousemove=e=>{const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)*W/r.width;if(px<L){svg.onmouseleave();return}
  const t=t0+(px-L)/pw*(t1-t0);let rows=[],best=null;
  for(const se of series){let p=null,d=1e18;for(const q of se.pts){const dd=Math.abs(q[0]-t);if(dd<d){d=dd;p=q}}if(p&&d<=(o.snap||1e18)){rows.push('<i style="background:'+se.color+'"></i>'+esc(se.label)+': <b>'+o.fmt(p[1])+'</b>');best=best??p[0]}}
  if(best==null){svg.onmouseleave();return}xh.setAttribute('x1',X(best));xh.setAttribute('x2',X(best));xh.style.display='';
  tip.innerHTML='<div class="mut">'+(o.long?hm(best):hms(best))+'</div>'+rows.join('<br>');tip.style.display='block';
  const lx=X(best)*r.width/W;tip.style.left=Math.min(lx+12,r.width-tip.offsetWidth-4)+'px';tip.style.top='6px'};
 svg.onmouseleave=()=>{tip.style.display='none';xh.style.display='none'}}
const legend=items=>'<div class="legend">'+items.map(i=>'<span><span class="sw" style="background:'+i[1]+'"></span>'+esc(i[0])+'</span>').join('')+'</div>';
const pill=(id,val)=>{$(id).querySelector('b').textContent=val};

function render(d){
 const st=LV[d.status],trek=!!$('theme').selectedOptions[0]?.hasAttribute('data-startrek');  // set in the theme file header
 // header badge: alarm words only in the Star Trek themes; elsewhere hidden (screen readers still get the status)
 $('alarm').className='alarm'+(d.status=='crit'||d.status=='warn'?' '+d.status:'')+(trek?'':' sr');$('alarm').style.setProperty('--c',st[2]);
 $('alarm').textContent=st[3];
 $('upd').textContent='Aktualisiert '+hms(d.time)+' · Intervall __POLL_S__ s'+(d.collector_age_min!=null?' · Verlauf vor '+d.collector_age_min+' min gesichert':'');
 $('sd').textContent=(trek?'Sternzeit '+stardate(d.time):new Date(d.time).toLocaleDateString('de-AT',{weekday:'long',day:'numeric',month:'long',year:'numeric'}))+' · FRITZ!Box-Mesh';
 $('r-status').style.setProperty('--c',st[2]);$('r-status').classList.toggle('warn',d.status=='warn');pill('r-status',st[1]);
 const i=d.internet,u=d.uplink,ap=(b,n)=>b.aps.find(a=>a.ap==n)||{},maxU=b=>Math.max(...b.aps.map(a=>a.util??0));
 pill('r-inet',num(i.sync_down,1));pill('r-24',num(maxU(d.bands[0]))+' %');pill('r-5',num(maxU(d.bands[1]))+' %');
 pill('r-dev',d.devices.filter(x=>x.online).length);pill('r-log',d.events.length?hm(d.events[0].ts):'–');
 renderAlerts(d.alerts);
 const kpi=(c,h,big,sm)=>'<div class="kpi" style="--c:'+c+'"><h3>'+h+'</h3><div class="big">'+big+'</div><div class="sm">'+sm+'</div></div>';
 const bandK=(b,c)=>{const f=ap(b,'FRITZ!Box'),r=ap(b,'Repeater');return kpi(c,b.label+' · Auslastung',num(f.util)+' % <small>/ '+num(r.util)+' %</small>','Box / Repeater · '+((f.clients||0)+(r.clients||0))+' Geräte · Kanal '+(f.channel??'–'))};
 $('kpis').innerHTML=kpi('var(--pe)','Internet',i.connected&&i.dsl_up?'Verbunden':'Getrennt','seit '+dur(i.uptime_s)+' · DSL '+num(i.sync_down,1)+' / '+num(i.sync_up,1)+' Mbit/s · Störabstand '+num(i.snr,1)+' dB')
  +kpi('var(--o2)','Datenverkehr','↓ '+rate(i.traffic.down),'↑ '+rate(i.traffic.up)+' · gesamter Anschluss')
  +bandK(d.bands[0],'var(--li)')+bandK(d.bands[1],'var(--pw)')
  +kpi('var(--sk)','Repeater-Anbindung',u?num(u.down)+' <small>Mbit/s</small>':'Getrennt',u?'Signal '+num(u.rssi)+' dBm · 5 GHz zur FRITZ!Box':'keine 5-GHz-Verbindung');
 for(const b of d.bands){const id=b.band=='2.4'?'s-24':'s-5',c=b.band=='2.4'?'var(--li)':'var(--pw)',f=ap(b,'FRITZ!Box'),r=ap(b,'Repeater');
  $(id).innerHTML='<h2 class="hb" style="--c:'+c+'">Funk '+b.label+'</h2><h4>Kanalauslastung · 24 Stunden</h4>'+legend([['FRITZ!Box','var(--s1)'],['Repeater','var(--s2)'],['Warnschwelle '+d.thr_util+' %','var(--thr,var(--warn))']])+'<div class="chart" id="u'+b.band+'"></div>'
   +'<h4 style="margin-top:12px">Internet-Datenverkehr · 10 Minuten</h4>'+legend([['Download','var(--s1)'],['Upload','var(--s2)']])+'<div class="chart" id="t'+b.band+'"></div>'
   +'<div class="note">Rauschen FRITZ!Box '+num(f.noise)+' dBm · Repeater '+num(r.noise)+' dBm</div>';
  const t1=d.time,t0=t1-86400000,tk=[0,6,12,18,24].map(h=>t0+h*3600000);
  lineChart($('u'+b.band),[{label:'FRITZ!Box',color:'var(--s1)',pts:f.util_hist||[]},{label:'Repeater',color:'var(--s2)',pts:r.util_hist||[]}],
   {t0,t1,ticks:tk,max:100,thr:d.thr_util,fmt:v=>v+' %',fmtAxis:v=>v+' %',long:true,snap:1800000,aria:b.label+': Kanalauslastung der letzten 24 Stunden in Prozent'});
  const ta=d.t_axis,z=k=>ta.map((t,j)=>[t,b[k][j]]);
  lineChart($('t'+b.band),[{label:'Download',color:'var(--s1)',pts:z('down')},{label:'Upload',color:'var(--s2)',pts:z('up')}],
   {t0:ta[0],t1:ta[ta.length-1],ticks:[ta[0],ta[Math.floor(ta.length/2)],ta[ta.length-1]],minMax:0.1,fmt:rate,fmtAxis:v=>v&&v<1?num(v*1000)+' k':num(v),aria:b.label+': Datenverkehr der letzten 10 Minuten in Mbit/s'})}
 $('devs').innerHTML='<table><thead><tr><th class="l">Status</th><th class="l">Gerät</th><th class="l">Verbindung</th><th>Signal</th><th>Rate ↓ / ↑</th><th>Verkehr ↓ / ↑</th><th>Abbrüche 1 h</th><th>Abbrüche 24 h</th><th>Wechsel 24 h</th></tr></thead><tbody>'
  +d.devices.map(x=>{const s=sb(x);return '<tr class="click'+(x.quiet?' quiet" title="Alarm '+(x.quiet=='muted'?'stummgeschaltet':'bestätigt'):'')+'" data-mac="'+esc(x.mac)+'" tabindex="0" style="--c:'+s[2]+'"><td class="l"><span class="st">'+s[1]+'</span></td>'
   +'<td class="l"><a href="#dev='+esc(x.mac)+'">'+esc(x.name)+'</a>'+(x.always_on?' <span class="mut" title="in fast allen Messungen der letzten 24 h verbunden">· dauerhaft</span>':'')+'</td>'
   +'<td class="l">'+(x.online?(x.band=='2.4'?'2,4':'5')+' GHz · '+x.ap:'zuletzt '+(x.last_seen?hm(x.last_seen):'–'))+'</td>'
   +'<td>'+(x.rssi==null?'–':x.rssi+' dBm')+'</td><td>'+num(x.rate_down)+' / '+num(x.rate_up)+'</td>'
   +'<td>'+(x.recorded?rate(x.traffic_down)+' / '+rate(x.traffic_up):'<span class="mut">nicht aufgezeichnet</span>')+'</td>'
   +'<td>'+x.drops_1h+'</td><td>'+x.drops_24h+'</td><td>'+x.roams_24h+'</td></tr>'}).join('')+'</tbody></table>'
  +'<div class="note">Für Details auf ein Gerät klicken. Rate = ausgehandeltes Tempo in Mbit/s (der Repeater meldet keine Upload-Rate). Abbrüche zählen Abmeldungen ohne Wechsel zu einem anderen Zugangspunkt; Wechsel zwischen FRITZ!Box und Repeater sind normal.</div>';
 const kind={login:'angemeldet',drop:'abgemeldet',roam:'gewechselt'};
 $('evs').innerHTML='<div class="log">'+d.events.map(e=>'<div class="t">'+hm(e.ts)+'</div><div>'+esc(e.name)+' '+kind[e.kind]+'</div><div class="mut">'+e.band+' · '+e.ap+'</div>').join('')+'</div>'}

let lastAlerts='',mutedOpen=false;
const btn=(lbl,act,ids,c,h)=>'<button class="btn" type="button" style="--b:'+c+'" data-act="'+act+'" data-ids="'+esc(JSON.stringify(ids))+'"'+(h?' data-h="'+h+'"':'')+'>'+lbl+'</button>';
const until=t=>new Date(t).toLocaleString('de-AT',new Date(t).toDateString()==new Date().toDateString()?{hour:'2-digit',minute:'2-digit'}:{weekday:'short',hour:'2-digit',minute:'2-digit'});
const row=(a,extra,acts)=>{const l=LV[a.level];return '<div class="alert '+(a.state||'')+'" style="--c:'+l[2]+'"><div class="tag"><span class="sr">'+l[3]+'</span></div><div class="txt"><b>'+esc(a.title)+'</b><span>'+esc(a.detail)+extra+'</span></div>'+(acts?'<div class="acts">'+acts+'</div>':'')+'<div class="end"></div></div>'};
const alertRows=list=>list.map(a=>a.state=='acked'?row(a,' · Bestätigt um '+hm(a.since),btn('Zurücknehmen','clear',[a.id],'var(--tan)'))
 :a.state=='muted'?row(a,' · stumm bis '+until(a.until),btn('Aufheben','clear',[a.id],'var(--tan)'))
 :row(a,'',btn('Bestätigen','ack',[a.id],'var(--o2)')+btn('Stumm 1 h','mute',[a.id],'var(--pw)',1)+btn('Stumm 24 h','mute',[a.id],'var(--li)',24))).join('');
const sb=x=>x.level==2?LV.crit:x.level==1?LV.warn:x.quiet?['',x.quiet=='muted'?'Stumm':'Bestätigt',x.quiet_level==2?'var(--crit)':x.quiet_level==1?'var(--warn)':'var(--info)']:x.online?LV.ok:['','Offline','var(--mut)'];
function renderAlerts(all){const key=JSON.stringify(all);if(key==lastAlerts)return;lastAlerts=key;
 const act=all.filter(a=>a.state=='active'),ack=all.filter(a=>a.state=='acked'),mut=all.filter(a=>a.state=='muted');
 let h=act.length>1?'<div class="allbar">'+btn('Alle bestätigen','ack',act.map(a=>a.id),'var(--o2)')+'</div>':'';
 h+=alertRows(act);
 if(!act.length)h+=row({level:'ok',title:'Keine aktiven Alarme',detail:ack.length||mut.length?'Alle offenen Hinweise sind bestätigt oder stummgeschaltet.':'Alle Prüfungen ohne Befund.'},'','');
 h+=alertRows(ack);
 if(mut.length)h+='<details class="muted"'+(mutedOpen?' open':'')+'><summary>Stummgeschaltet: '+mut.length+'</summary>'+alertRows(mut)+'</details>';
 $('alerts').innerHTML=h}

const date=t=>t?new Date(t).toLocaleString('de-AT',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}):'–';
const bytes=v=>v==null?'–':v>=1e9?num(v/1e9,2)+' GB':v>=1e6?num(v/1e6,1)+' MB':num(v/1e3)+' kB';
const yes=v=>v==null?'–':v?'ja':'nein';
function renderDetail(x){const D=x.detail||{},s=sb(x),k={login:'angemeldet',drop:'abgemeldet',roam:'gewechselt'},bl=b=>b=='2.4'?'2,4 GHz':'5 GHz';
 const kpi=(c,h,big,sm)=>'<div class="kpi" style="--c:'+c+'"><h3>'+h+'</h3><div class="big">'+big+'</div><div class="sm">'+sm+'</div></div>';
 const places={};for(const r of x.hist)places[r[5]+' · '+bl(r[6])]=(places[r[5]+' · '+bl(r[6])]||0)+1;
 const where=Object.entries(places).sort((a,b)=>b[1]-a[1]).map(([p,n])=>p+' '+Math.round(100*n/x.hist.length)+' %').join(', ')||'–';
 const info=[['Name',esc(x.name)],['Hostname',esc(D.hostname??'–')],['IPv4',esc(D.ip??'–')+(D.static_ip?' <span class="mut">· feste IP</span>':'')],
  ['IPv6',D.ipv6&&D.ipv6.length?D.ipv6.map(esc).join('<br>'):'–'],['MAC',esc(x.mac)+(D.random_mac?' <span class="mut">· zufällige (private) Adresse</span>':'')],
  ['Hersteller',esc(D.vendor??'–')],['Modell',esc(D.model??'–')],['Firmware',esc(D.firmware??'–')],
  ['WLAN-Standard',D.wifi?esc(D.wifi)+' <span class="mut">· 802.11 '+esc(D.phymodes.join('/'))+'</span>':'–'],
  ['Antennen',D.streams?D.streams+' Stream'+(D.streams>1?'s':'')+' · bis '+esc(D.width):'–'],['Sicherheit',esc(D.security??'–')],
  ['802.11k / v',yes(D.k)+' / '+yes(D.v)+' <span class="mut">· Hilfen für den Wechsel zwischen Box und Repeater</span>'],
  ['Verbunden seit',date(D.connected_since)],['Erstmals gesehen',date(D.first_seen)],['Zuletzt aktiv',date(D.last_used)],
  ['Latenz',D.latency_ms!=null?num(D.latency_ms,1)+' ms <span class="mut">· gemessen '+date(D.latency_at)+'</span>':'–'],
  ['WAN-Zähler',D.wan_rx!=null?'empfangen '+bytes(D.wan_rx)+' · gesendet '+bytes(D.wan_tx):'–'],
  ['Dauerhaft verbunden',x.always_on?'ja':'nein'],['Anteil Messungen 24 h',num(100*x.share)+' %'],['Verbindungsorte 24 h',where]];
 let h='<div class="dhead"><a class="btn" href="#s-dev" style="--b:var(--sk)">← Alle Geräte</a><span class="alarm'+(s[2]=='var(--warn)'?' warn':'')+'" style="--c:'+s[2]+'">'+esc(s[1])+'</span></div>'
  +'<h2 class="hb" style="--c:var(--sk)">'+esc(x.name)+'</h2>';
 if(x.alerts.length)h+=alertRows(x.alerts);
 h+='<div class="kpis">'+kpi('var(--li)','Verbindung',x.online?bl(x.band):'Offline',x.online?x.ap+' · Kanal '+(D.channel??'–'):'zuletzt '+(x.last_seen?date(x.last_seen):'–'))
  +kpi('var(--pw)','Signal',x.rssi==null?'–':x.rssi+' <small>dBm</small>','Störabstand '+(D.snr==null?'–':D.snr+' dB')+' · Verfügbarkeit '+(D.avail==null?'–':D.avail+' %'))
  +kpi('var(--o2)','Rate ↓ / ↑',num(x.rate_down)+' <small>/ '+num(x.rate_up)+' Mbit/s</small>','maximal '+num(D.max_down)+' / '+num(D.max_up)+' Mbit/s')
  +kpi('var(--pe)','Datenverkehr',x.recorded?'↓ '+rate(x.traffic_down):'–',x.recorded?'↑ '+rate(x.traffic_up):'nicht aufgezeichnet')
  +kpi('var(--sk)','Stabilität',x.drops_24h+' <small>Abbrüche 24 h</small>',x.drops_1h+' in 1 h · '+x.roams_24h+' Wechsel Box/Repeater')+'</div>';
 h+='<div class="cols"><section class="panel"><h2 class="hb" style="--c:var(--pw)">Signal · 24 Stunden</h2>'+legend([['Signal','var(--s1)'],['Warnschwelle '+x.thr_rssi+' dBm','var(--thr,var(--warn))']])+'<div class="chart" id="dsig"></div>'
  +'<h4 style="margin-top:12px">Ausgehandelte Rate · 24 Stunden</h4>'+legend([['Download','var(--s1)'],['Upload','var(--s2)']])+'<div class="chart" id="drate"></div>'
  +(D.traffic?'<h4 style="margin-top:12px">Datenverkehr · 10 Minuten</h4>'+legend([['Download','var(--s1)'],['Upload','var(--s2)']])+'<div class="chart" id="dtraf"></div>':'')
  +'<div class="note">Verlauf aus dem Sammeldienst (alle 15 min, nur bei wachem Mac).</div></section>'
  +'<section class="panel"><h2 class="hb" style="--c:var(--li)">Daten</h2><dl class="info">'+info.map(i=>'<dt>'+i[0]+'</dt><dd>'+i[1]+'</dd>').join('')+'</dl></section></div>';
 h+='<h2 class="hb" style="--c:var(--tan)">Ereignisse · 24 Stunden</h2>'+(x.events.length?'<div class="log" style="grid-template-columns:96px minmax(0,1fr) auto">'+x.events.map(e=>'<div class="t">'+until(e.ts)+'</div><div>'+k[e.kind]+'</div><div class="mut">'+e.band+' · '+e.ap+'</div>').join('')+'</div>':'<div class="mut">Keine An- oder Abmeldungen in den letzten 24 Stunden.</div>');
 $('s-detail').innerHTML=h;
 const t1=x.time,t0=t1-86400000,tk=[0,6,12,18,24].map(h=>t0+h*3600000),col=i=>x.hist.filter(r=>r[i]!=null).map(r=>[r[0],r[i]]);
 lineChart($('dsig'),[{label:'Signal',color:'var(--s1)',pts:col(1)}],{t0,t1,ticks:tk,min:-100,max:-20,thr:x.thr_rssi,fmt:v=>v+' dBm',fmtAxis:v=>v,long:true,snap:1800000,aria:'Signalstärke der letzten 24 Stunden in dBm'});
 lineChart($('drate'),[{label:'Download',color:'var(--s1)',pts:col(3)},{label:'Upload',color:'var(--s2)',pts:col(4)}],{t0,t1,ticks:tk,fmt:v=>num(v)+' Mbit/s',fmtAxis:v=>num(v),long:true,snap:1800000,aria:'Ausgehandelte Rate der letzten 24 Stunden in Mbit/s'});
 if(D.traffic){const ta=x.t_axis,z=a=>a.slice(-ta.length).map((v,j,arr)=>[ta[ta.length-arr.length+j],v]);
  lineChart($('dtraf'),[{label:'Download',color:'var(--s1)',pts:z(D.traffic.down)},{label:'Upload',color:'var(--s2)',pts:z(D.traffic.up)}],
   {t0:ta[0],t1:ta[ta.length-1],ticks:[ta[0],ta[Math.floor(ta.length/2)],ta[ta.length-1]],minMax:0.1,fmt:rate,fmtAxis:v=>v&&v<1?num(v*1000)+' k':num(v),aria:'Datenverkehr der letzten 10 Minuten in Mbit/s'})}}

document.addEventListener('click',async e=>{const b=e.target.closest('button[data-act]');if(!b)return;b.disabled=true;
 try{const r=await fetch('/ack',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard':'1'},body:JSON.stringify({ids:JSON.parse(b.dataset.ids),action:b.dataset.act,hours:+b.dataset.h||0})});if(!r.ok)throw new Error('HTTP '+r.status)}
 catch(err){$('upd').innerHTML='<span class="err">Aktion fehlgeschlagen: '+esc(err.message)+'</span>'}lastAlerts='';tick()});
const openDev=e=>{const tr=e.target.closest('tr[data-mac]');if(!tr||e.target.closest('a'))return;if(e.type=='keydown'&&e.key!='Enter')return;location.hash='dev='+tr.dataset.mac};
$('devs').addEventListener('click',openDev);$('devs').addEventListener('keydown',openDev);
document.addEventListener('toggle',e=>{if(e.target.classList&&e.target.classList.contains('muted'))mutedOpen=e.target.open},true);
const devMac=()=>(location.hash.match(/^#dev=([0-9A-Fa-f:]{17})$/)||[])[1];
let last=null,lastDev=null;
async function tick(){const mac=devMac();
 try{const r=await fetch('/data',{cache:'no-store'}),d=await r.json();if(d.error)throw new Error(d.error);last=d;render(d)}
 catch(e){$('upd').innerHTML='<span class="err">Keine Daten von der FRITZ!Box: '+esc(e.message)+'</span>'}
 if(mac){try{const r=await fetch('/device?mac='+encodeURIComponent(mac),{cache:'no-store'}),x=await r.json();if(mac!=devMac())return;
   if(x.error)$('s-detail').innerHTML='<div class="dhead"><a class="btn" href="#s-dev" style="--b:var(--sk)">← Alle Geräte</a></div><p class="err">'+esc(x.error)+'</p>';else{lastDev=x;renderDetail(x)}}
  catch(e){$('upd').innerHTML='<span class="err">Gerätedaten nicht verfügbar: '+esc(e.message)+'</span>'}}}
addEventListener('hashchange',()=>{lastDev=null;const mac=devMac();$('s-detail').hidden=!mac;$('overview').hidden=!!mac;
 if(mac){$('s-detail').innerHTML='<div class="dhead sub">Lade Gerät …</div>';scrollTo(0,0)}else if(location.hash.length>1)document.getElementById(location.hash.slice(1))?.scrollIntoView();tick()});
const rerender=()=>{last&&render(last);devMac()&&lastDev&&renderDetail(lastDev)};
addEventListener('resize',rerender);
$('theme').value=document.documentElement.dataset.theme;if(!$('theme').value)$('theme').value=document.documentElement.dataset.theme='lcars';  // unknown or removed theme
$('theme').addEventListener('change',e=>{document.documentElement.dataset.theme=e.target.value;try{localStorage.setItem('wlan-theme',e.target.value)}catch(_){}rerender()});dispatchEvent(new HashChangeEvent('hashchange'));setInterval(tick,__POLL_S__*1000);

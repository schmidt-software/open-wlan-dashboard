const $=id=>document.getElementById(id),esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let LANG=I18N[document.documentElement.lang]?document.documentElement.lang:'en';  // set before first paint in index.html
const loc=()=>({de:'de-AT',en:'en-GB'})[LANG]||LANG;  // number and date format
const T=(k,a={})=>(I18N[LANG][k]??I18N.en[k]??k).replace(/\{(\w+)\}/g,(_,n)=>a[n]??'');  // texts in web/i18n.js
const LVC={crit:['!','var(--crit)'],warn:['!','var(--warn)'],info:['i','var(--info)'],ok:['✓','var(--good)']};
const lv=l=>[LVC[l][0],T('lv.'+l),LVC[l][1],T('lv.'+l+'.l')];  // [icon, short word, colour, long word]
const num=(v,d=0)=>v==null?'–':v.toLocaleString(loc(),{maximumFractionDigits:d,minimumFractionDigits:d});
const bl=b=>b=='2.4'?num(2.4,1)+' GHz':'5 GHz';
const rate=v=>v==null?'–':v>=10?num(v,1)+' Mbit/s':v>=1?num(v,2)+' Mbit/s':num(v*1000)+' kbit/s';
const hm=t=>new Date(t).toLocaleTimeString(loc(),{hour:'2-digit',minute:'2-digit'});
const hms=t=>new Date(t).toLocaleTimeString(loc());
const dur=s=>{const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?d+' '+T('unit.day')+' '+h+' h':h?h+' h '+m+' min':m+' min'};
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
 const st=lv(d.status),trek=!!$('theme').selectedOptions[0]?.hasAttribute('data-starship');  // set in the theme file header
 // header badge: alarm words only in the starship themes; elsewhere hidden (screen readers still get the status)
 $('alarm').className='alarm'+(d.status=='crit'||d.status=='warn'?' '+d.status:'')+(trek?'':' sr');$('alarm').style.setProperty('--c',st[2]);
 $('alarm').textContent=st[3];
 $('upd').textContent=T('upd',{time:hms(d.time),poll:__POLL_S__})+(d.collector_age_min!=null?T('upd.hist',{min:d.collector_age_min}):'');
 $('sd').textContent=(trek?T('stardate')+' '+stardate(d.time):new Date(d.time).toLocaleDateString(loc(),{weekday:'long',day:'numeric',month:'long',year:'numeric'}))+' · '+T('mesh');
 $('r-status').style.setProperty('--c',st[2]);$('r-status').classList.toggle('warn',d.status=='warn');pill('r-status',st[1]);
 const i=d.internet,u=d.uplink,ap=(b,n)=>b.aps.find(a=>a.ap==n)||{},maxU=b=>Math.max(...b.aps.map(a=>a.util??0));
 pill('r-inet',num(i.sync_down,1));pill('r-24',num(maxU(d.bands[0]))+' %');pill('r-5',num(maxU(d.bands[1]))+' %');
 pill('r-dev',d.devices.filter(x=>x.online).length);pill('r-log',d.events.length?hm(d.events[0].ts):'–');
 renderAlerts(d.alerts);
 const kpi=(c,h,big,sm)=>'<div class="kpi" style="--c:'+c+'"><h3>'+h+'</h3><div class="big">'+big+'</div><div class="sm">'+sm+'</div></div>';
 const bandK=(b,c)=>{const f=ap(b,'FRITZ!Box'),r=ap(b,'Repeater');return kpi(c,T('k.util',{band:bl(b.band)}),num(f.util)+'&nbsp;% <small>/&nbsp;'+num(r.util)+'&nbsp;%</small>',T('k.util.sm',{n:(f.clients||0)+(r.clients||0),ch:f.channel??'–'}))};
 $('kpis').innerHTML=kpi('var(--pe)',T('k.inet'),T(i.connected&&i.dsl_up?'connected':'disconnected'),T('k.inet.sm',{dur:dur(i.uptime_s),down:num(i.sync_down,1),up:num(i.sync_up,1),snr:num(i.snr,1)}))
  +kpi('var(--o2)',T('k.traffic'),'↓ '+rate(i.traffic.down),T('k.traffic.sm',{up:rate(i.traffic.up)}))
  +bandK(d.bands[0],'var(--li)')+bandK(d.bands[1],'var(--pw)')
  +kpi('var(--sk)',T('k.uplink'),u?num(u.down)+' <small>Mbit/s</small>':T('disconnected'),u?T('k.uplink.sm',{rssi:num(u.rssi)}):T('k.uplink.none'));
 for(const b of d.bands){const id=b.band=='2.4'?'s-24':'s-5',c=b.band=='2.4'?'var(--li)':'var(--pw)',f=ap(b,'FRITZ!Box'),r=ap(b,'Repeater');
  $(id).innerHTML='<h2 class="hb" style="--c:'+c+'">'+T('radio',{band:bl(b.band)})+'</h2><h4>'+T('ch.load')+'</h4>'+legend([['FRITZ!Box','var(--s1)'],['Repeater','var(--s2)'],[T('threshold',{v:d.thr_util+' %'}),'var(--thr,var(--warn))']])+'<div class="chart" id="u'+b.band+'"></div>'
   +'<h4 style="margin-top:12px">'+T('inet.traffic')+'</h4>'+legend([['Download','var(--s1)'],['Upload','var(--s2)']])+'<div class="chart" id="t'+b.band+'"></div>'
   +'<div class="note">'+T('noise.note',{box:num(f.noise),rep:num(r.noise)})+'</div>';
  const t1=d.time,t0=t1-86400000,tk=[0,6,12,18,24].map(h=>t0+h*3600000);
  lineChart($('u'+b.band),[{label:'FRITZ!Box',color:'var(--s1)',pts:f.util_hist||[]},{label:'Repeater',color:'var(--s2)',pts:r.util_hist||[]}],
   {t0,t1,ticks:tk,max:100,thr:d.thr_util,fmt:v=>v+' %',fmtAxis:v=>v+' %',long:true,snap:1800000,aria:T('aria.util',{band:bl(b.band)})});
  const ta=d.t_axis,z=k=>ta.map((t,j)=>[t,b[k][j]]);
  lineChart($('t'+b.band),[{label:'Download',color:'var(--s1)',pts:z('down')},{label:'Upload',color:'var(--s2)',pts:z('up')}],
   {t0:ta[0],t1:ta[ta.length-1],ticks:[ta[0],ta[Math.floor(ta.length/2)],ta[ta.length-1]],minMax:0.1,fmt:rate,fmtAxis:v=>v&&v<1?num(v*1000)+' k':num(v),aria:T('aria.traffic',{band:bl(b.band)})})}
 $('devs').innerHTML='<table><thead><tr>'+['status','device','conn'].map(k=>'<th class="l">'+T('th.'+k)+'</th>').join('')+['signal','rate','traffic','drops1','drops24','roams'].map(k=>'<th>'+T('th.'+k)+'</th>').join('')+'</tr></thead><tbody>'
  +d.devices.map(x=>{const s=sb(x);return '<tr class="click'+(x.quiet?' quiet" title="'+T(x.quiet=='muted'?'tip.muted':'tip.acked'):'')+'" data-mac="'+esc(x.mac)+'" tabindex="0" style="--c:'+s[2]+'"><td class="l"><span class="st">'+s[1]+'</span></td>'
   +'<td class="l"><a href="#dev='+esc(x.mac)+'">'+esc(x.name)+'</a>'+(x.always_on?' <span class="mut" title="'+T('always.tip')+'">· '+T('always')+'</span>':'')
   +(x.prio_now?' <b title="'+T('prio.tip')+'">· '+T('prio')+'</b>':x.prio_24h?' <span class="mut" title="'+T('prio.tip')+'">· '+T('prio.24',{n:x.prio_24h})+'</span>':'')+'</td>'
   +'<td class="l">'+(x.online?bl(x.band)+' · '+x.ap:T('last.seen',{t:x.last_seen?hm(x.last_seen):'–'}))+'</td>'
   +'<td>'+(x.rssi==null?'–':x.rssi+' dBm')+'</td><td>'+num(x.rate_down)+' / '+num(x.rate_up)+'</td>'
   +'<td>'+(x.recorded?rate(x.traffic_down)+' / '+rate(x.traffic_up):'<span class="mut">'+T('not.recorded')+'</span>')+'</td>'
   +'<td>'+x.drops_1h+'</td><td>'+x.drops_24h+'</td><td>'+x.roams_24h+'</td></tr>'}).join('')+'</tbody></table>'
  +'<div class="note">'+T('dev.note')+'</div>';
 $('evs').innerHTML='<div class="log">'+d.events.map(e=>'<div class="t">'+hm(e.ts)+'</div><div>'+esc(e.name)+' '+T('ev.'+e.kind)+'</div><div class="mut">'+bl(e.band)+' · '+e.ap+'</div>').join('')+'</div>'}

let lastAlerts='',mutedOpen=false;
const btn=(lbl,act,ids,c,h)=>'<button class="btn" type="button" style="--b:'+c+'" data-act="'+act+'" data-ids="'+esc(JSON.stringify(ids))+'"'+(h?' data-h="'+h+'"':'')+'>'+lbl+'</button>';
const until=t=>new Date(t).toLocaleString(loc(),new Date(t).toDateString()==new Date().toDateString()?{hour:'2-digit',minute:'2-digit'}:{weekday:'short',hour:'2-digit',minute:'2-digit'});
const fmtArg=(k,v)=>k=='time'?String(v).replace(/^(\d\d)\.(\d\d)\.(\d\d) /,LANG=='de'?'$1.$2.$3 ':'$1/$2/$3 '):k=='band'?bl(v):k=='boot'?date(v):k=='kind'?T('nk.'+v):k=='snr'?num(v,1):typeof v=='number'?num(v):v;
const atext=(a,p)=>{const f={};for(const k in a.args)f[k]=fmtArg(k,a.args[k]);return T('a.'+a.key+'.'+p,f)};  // alerts come as key + args from dashboard.py
const row=(a,extra,acts)=>{const l=lv(a.level);if(a.key)a={...a,title:atext(a,'t'),detail:atext(a,'d')};return '<div class="alert '+(a.state||'')+'" style="--c:'+l[2]+'"><div class="tag"><span class="sr">'+l[3]+'</span></div><div class="txt"><b>'+esc(a.title)+'</b><span>'+esc(a.detail)+extra+'</span></div>'+(acts?'<div class="acts">'+acts+'</div>':'')+'<div class="end"></div></div>'};
const alertRows=list=>list.map(a=>a.state=='acked'?row(a,T('acked.at',{t:hm(a.since)}),btn(T('b.undo'),'clear',[a.id],'var(--tan)'))
 :a.state=='muted'?row(a,T('muted.until',{t:until(a.until)}),btn(T('b.unmute'),'clear',[a.id],'var(--tan)'))
 :row(a,'',btn(T('b.ack'),'ack',[a.id],'var(--o2)')+btn(T('b.mute1'),'mute',[a.id],'var(--pw)',1)+btn(T('b.mute24'),'mute',[a.id],'var(--li)',24))).join('');
const sb=x=>x.level==2?lv('crit'):x.level==1?lv('warn'):x.quiet?['',T(x.quiet=='muted'?'st.muted':'st.acked'),x.quiet_level==2?'var(--crit)':x.quiet_level==1?'var(--warn)':'var(--info)']:x.online?lv('ok'):['',T('st.offline'),'var(--mut)'];
function renderAlerts(all){const key=JSON.stringify(all);if(key==lastAlerts)return;lastAlerts=key;
 const act=all.filter(a=>a.state=='active'),ack=all.filter(a=>a.state=='acked'),mut=all.filter(a=>a.state=='muted');
 let h=act.length>1?'<div class="allbar">'+btn(T('b.ackall'),'ack',act.map(a=>a.id),'var(--o2)')+'</div>':'';
 h+=alertRows(act);
 if(!act.length)h+=row({level:'ok',title:T('no.alerts'),detail:T(ack.length||mut.length?'all.quiet':'all.ok')},'','');
 h+=alertRows(ack);
 if(mut.length)h+='<details class="muted"'+(mutedOpen?' open':'')+'><summary>'+T('muted.n',{n:mut.length})+'</summary>'+alertRows(mut)+'</details>';
 $('alerts').innerHTML=h}

const date=t=>t?new Date(t).toLocaleString(loc(),{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}):'–';
const bytes=v=>v==null?'–':v>=1e9?num(v/1e9,2)+' GB':v>=1e6?num(v/1e6,1)+' MB':num(v/1e3)+' kB';
const yes=v=>v==null?'–':T(v?'yes':'no');
function renderDetail(x){const D=x.detail||{},s=sb(x);
 const kpi=(c,h,big,sm)=>'<div class="kpi" style="--c:'+c+'"><h3>'+h+'</h3><div class="big">'+big+'</div><div class="sm">'+sm+'</div></div>';
 const places={};for(const r of x.hist)places[r[5]+' · '+bl(r[6])]=(places[r[5]+' · '+bl(r[6])]||0)+1;
 const where=Object.entries(places).sort((a,b)=>b[1]-a[1]).map(([p,n])=>p+' '+Math.round(100*n/x.hist.length)+' %').join(', ')||'–';
 const mut=t=>' <span class="mut">· '+t+'</span>';
 const info=[[T('i.name'),esc(x.name)],[T('i.host'),esc(D.hostname??'–')],['IPv4',esc(D.ip??'–')+(D.static_ip?mut(T('i.fixed')):'')],
  ['IPv6',D.ipv6&&D.ipv6.length?D.ipv6.map(esc).join('<br>'):'–'],['MAC',esc(x.mac)+(D.random_mac?mut(T('i.random')):'')],
  [T('i.vendor'),esc(D.vendor??'–')],[T('i.model'),esc(D.model??'–')],[T('i.fw'),esc(D.firmware??'–')],
  [T('i.wifi'),D.wifi?esc(D.wifi)+mut('802.11 '+esc(D.phymodes.join('/'))):'–'],
  [T('i.antennas'),D.streams?T('i.streams',{n:D.streams+' Stream'+(D.streams>1?'s':''),w:esc(D.width)}):'–'],[T('i.security'),esc(D.security??'–')],
  ['802.11k / v',yes(D.k)+' / '+yes(D.v)+mut(T('i.kv.note'))],
  [T('i.since'),date(D.connected_since)],[T('i.first'),date(D.first_seen)],[T('i.last'),date(D.last_used)],
  [T('i.latency'),D.latency_ms!=null?num(D.latency_ms,1)+' ms'+mut(T('i.measured',{t:date(D.latency_at)})):'–'],
  [T('i.wan'),D.wan_rx!=null?T('i.wan.v',{rx:bytes(D.wan_rx),tx:bytes(D.wan_tx)}):'–'],
  [T('i.prio'),x.prio_24h==null?'–':(x.prio_now?T('prio')+' · ':'')+T('prio.24',{n:x.prio_24h})+mut(T('prio.tip'))],
  [T('i.always'),yes(x.always_on)],[T('i.share'),num(100*x.share)+' %'],[T('i.places'),where]];
 let h='<div class="dhead"><a class="btn" href="#s-dev" style="--b:var(--sk)">← '+T('back')+'</a><span class="alarm'+(s[2]=='var(--warn)'?' warn':'')+'" style="--c:'+s[2]+'">'+esc(s[1])+'</span></div>'
  +'<h2 class="hb" style="--c:var(--sk)">'+esc(x.name)+'</h2>';
 if(x.alerts.length)h+=alertRows(x.alerts);
 h+='<div class="kpis">'+kpi('var(--li)',T('d.conn'),x.online?bl(x.band):T('st.offline'),x.online?T('d.conn.sm',{ap:x.ap,ch:D.channel??'–'}):T('last.seen',{t:x.last_seen?date(x.last_seen):'–'}))
  +kpi('var(--pw)',T('th.signal'),x.rssi==null?'–':x.rssi+' <small>dBm</small>',T('d.signal.sm',{snr:D.snr==null?'–':D.snr+' dB',avail:D.avail==null?'–':D.avail+' %'}))
  +kpi('var(--o2)',T('th.rate'),num(x.rate_down)+' <small>/ '+num(x.rate_up)+' Mbit/s</small>',T('d.rate.sm',{down:num(D.max_down),up:num(D.max_up)}))
  +kpi('var(--pe)',T('k.traffic'),x.recorded?'↓ '+rate(x.traffic_down):'–',x.recorded?'↑ '+rate(x.traffic_up):T('not.recorded'))
  +kpi('var(--sk)',T('d.stab'),x.drops_24h+' <small>'+T('d.drops24')+'</small>',T('d.stab.sm',{d1:x.drops_1h,r:x.roams_24h}))+'</div>';
 h+='<div class="cols"><section class="panel"><h2 class="hb" style="--c:var(--pw)">'+T('d.sig24')+'</h2>'+legend([['Signal','var(--s1)'],[T('threshold',{v:x.thr_rssi+' dBm'}),'var(--thr,var(--warn))']])+'<div class="chart" id="dsig"></div>'
  +'<h4 style="margin-top:12px">'+T('d.rate24')+'</h4>'+legend([['Download','var(--s1)'],['Upload','var(--s2)']])+'<div class="chart" id="drate"></div>'
  +(D.traffic?'<h4 style="margin-top:12px">'+T('d.traffic10')+'</h4>'+legend([['Download','var(--s1)'],['Upload','var(--s2)']])+'<div class="chart" id="dtraf"></div>':'')
  +'<div class="note">'+T('d.hist.note')+'</div></section>'
  +'<section class="panel"><h2 class="hb" style="--c:var(--li)">'+T('d.data')+'</h2><dl class="info">'+info.map(i=>'<dt>'+i[0]+'</dt><dd>'+i[1]+'</dd>').join('')+'</dl></section></div>';
 h+='<h2 class="hb" style="--c:var(--tan)">'+T('d.events24')+'</h2>'+(x.events.length?'<div class="log" style="grid-template-columns:96px minmax(0,1fr) auto">'+x.events.map(e=>'<div class="t">'+until(e.ts)+'</div><div>'+T('ev.'+e.kind)+'</div><div class="mut">'+bl(e.band)+' · '+e.ap+'</div>').join('')+'</div>':'<div class="mut">'+T('d.no.events')+'</div>');
 $('s-detail').innerHTML=h;
 const t1=x.time,t0=t1-86400000,tk=[0,6,12,18,24].map(h=>t0+h*3600000),col=i=>x.hist.filter(r=>r[i]!=null).map(r=>[r[0],r[i]]);
 lineChart($('dsig'),[{label:'Signal',color:'var(--s1)',pts:col(1)}],{t0,t1,ticks:tk,min:-100,max:-20,thr:x.thr_rssi,fmt:v=>v+' dBm',fmtAxis:v=>v,long:true,snap:1800000,aria:T('aria.sig')});
 lineChart($('drate'),[{label:'Download',color:'var(--s1)',pts:col(3)},{label:'Upload',color:'var(--s2)',pts:col(4)}],{t0,t1,ticks:tk,fmt:v=>num(v)+' Mbit/s',fmtAxis:v=>num(v),long:true,snap:1800000,aria:T('aria.rate')});
 if(D.traffic){const ta=x.t_axis,z=a=>a.slice(-ta.length).map((v,j,arr)=>[ta[ta.length-arr.length+j],v]);
  lineChart($('dtraf'),[{label:'Download',color:'var(--s1)',pts:z(D.traffic.down)},{label:'Upload',color:'var(--s2)',pts:z(D.traffic.up)}],
   {t0:ta[0],t1:ta[ta.length-1],ticks:[ta[0],ta[Math.floor(ta.length/2)],ta[ta.length-1]],minMax:0.1,fmt:rate,fmtAxis:v=>v&&v<1?num(v*1000)+' k':num(v),aria:T('aria.dtraffic')})}}

document.addEventListener('click',async e=>{const b=e.target.closest('button[data-act]');if(!b)return;b.disabled=true;
 try{const r=await fetch('/ack',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard':'1'},body:JSON.stringify({ids:JSON.parse(b.dataset.ids),action:b.dataset.act,hours:+b.dataset.h||0})});if(!r.ok)throw new Error('HTTP '+r.status)}
 catch(err){$('upd').innerHTML='<span class="err">'+esc(T('err.action',{m:err.message}))+'</span>'}lastAlerts='';tick()});
const openDev=e=>{const tr=e.target.closest('tr[data-mac]');if(!tr||e.target.closest('a'))return;if(e.type=='keydown'&&e.key!='Enter')return;location.hash='dev='+tr.dataset.mac};
$('devs').addEventListener('click',openDev);$('devs').addEventListener('keydown',openDev);
document.addEventListener('toggle',e=>{if(e.target.classList&&e.target.classList.contains('muted'))mutedOpen=e.target.open},true);
const devMac=()=>(location.hash.match(/^#dev=([0-9A-Fa-f:]{17})$/)||[])[1];
let last=null,lastDev=null;
async function tick(){const mac=devMac();
 try{const r=await fetch('/data',{cache:'no-store'}),d=await r.json();if(d.error)throw new Error(d.error);last=d;render(d)}
 catch(e){$('upd').innerHTML='<span class="err">'+esc(T('err.data',{m:e.message}))+'</span>'}
 if(mac){try{const r=await fetch('/device?mac='+encodeURIComponent(mac),{cache:'no-store'}),x=await r.json();if(mac!=devMac())return;
   if(x.error)$('s-detail').innerHTML='<div class="dhead"><a class="btn" href="#s-dev" style="--b:var(--sk)">← '+T('back')+'</a></div><p class="err">'+esc(I18N.en['err.'+x.error]?T('err.'+x.error):x.error)+'</p>';else{lastDev=x;renderDetail(x)}}
  catch(e){$('upd').innerHTML='<span class="err">'+esc(T('err.device',{m:e.message}))+'</span>'}}}
addEventListener('hashchange',()=>{lastDev=null;const mac=devMac();$('s-detail').hidden=!mac;$('overview').hidden=!!mac;
 if(mac){$('s-detail').innerHTML='<div class="dhead sub">'+T('loading.device')+'</div>';scrollTo(0,0)}else if(location.hash.length>1)document.getElementById(location.hash.slice(1))?.scrollIntoView();tick()});
const rerender=()=>{last&&render(last);devMac()&&lastDev&&renderDetail(lastDev)};
addEventListener('resize',rerender);
$('theme').value=document.documentElement.dataset.theme;if(!$('theme').value)$('theme').value=document.documentElement.dataset.theme='desktop';  // unknown or removed theme: the default
$('theme').addEventListener('change',e=>{document.documentElement.dataset.theme=e.target.value;try{localStorage.setItem('wlan-theme',e.target.value)}catch(_){}rerender()});
function applyLang(){document.documentElement.lang=LANG;document.title=T('title');
 document.querySelectorAll('[data-i18n]').forEach(e=>e.textContent=T(e.dataset.i18n));
 document.querySelectorAll('[data-i18n-aria]').forEach(e=>e.setAttribute('aria-label',T(e.dataset.i18nAria)));
 $('r-24').querySelector('span').textContent=bl('2.4');
 const sel=$('theme'),opts=[...sel.options];opts.forEach(o=>o.textContent=o.dataset[LANG]||o.dataset.en);
 opts.sort((a,b)=>(a.value!='starship')-(b.value!='starship')||a.textContent.localeCompare(b.textContent,loc())).forEach(o=>sel.append(o))}  // starship (the base design) first
$('lang').value=LANG;applyLang();
$('lang').addEventListener('change',e=>{LANG=e.target.value;try{localStorage.setItem('wlan-lang',LANG)}catch(_){}applyLang();lastAlerts='';rerender();tick()});  // tick also redraws a device error message
dispatchEvent(new HashChangeEvent('hashchange'));setInterval(tick,__POLL_S__*1000);

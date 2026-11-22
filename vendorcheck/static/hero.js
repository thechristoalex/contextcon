/* shared abstract hero animation (Baselayer style) */
function heroAnim(){
  const T=8, W=1120, H=400, cx=560, cy=200;
  // left: stacked isometric layers (data sources), deeper = fainter
  const layers=[0,1,2,3].map(i=>({x:210,y:118+i*46,o:1-i*0.18}));
  const rh=(x,y,w=118,h=40)=>`M${x-w} ${y} L${x} ${y-h} L${x+w} ${y} L${x} ${y+h} Z`;
  // centre: ring segments = checks
  const segs=6, R=74, seg=(i)=>{const a0=(-90+i*60+5)*Math.PI/180,a1=(-90+i*60+55)*Math.PI/180;
    return `M${(cx+R*Math.cos(a0)).toFixed(1)} ${(cy+R*Math.sin(a0)).toFixed(1)} A${R} ${R} 0 0 1 ${(cx+R*Math.cos(a1)).toFixed(1)} ${(cy+R*Math.sin(a1)).toFixed(1)}`};
  const kf=(n,a)=>`@keyframes ${n}{0%,${a}%{opacity:.12}${a+4}%,88%{opacity:1}96%,100%{opacity:.12}}`;
  let css=`.ha .flow{stroke-dasharray:2 22;animation:flow 1.6s linear infinite}@keyframes flow{to{stroke-dashoffset:-48}}
   .ha .seg{opacity:.12;animation-duration:${T}s;animation-iteration-count:infinite}
   .ha .lay{animation:lay ${T}s ease-in-out infinite}@keyframes lay{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
   .ha .spin{transform-origin:${cx}px ${cy}px;animation:spin 24s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
   .ha .core{transform-origin:${cx}px ${cy}px;animation:core ${T}s ease-in-out infinite}@keyframes core{0%,40%{transform:scale(.85);opacity:.6}52%,88%{transform:scale(1);opacity:1}100%{transform:scale(.85);opacity:.6}}
   .ha .land{animation:land ${T}s ease-out infinite}@keyframes land{0%,71%{opacity:.25}75%,90%{opacity:1}100%{opacity:.25}}
   .ha .rip{transform-origin:910px 200px;transform-box:view-box;animation:rip ${T}s ease-out infinite;opacity:0}@keyframes rip{0%,72%{transform:scale(.4);opacity:0}74%{opacity:.9}92%,100%{transform:scale(2.2);opacity:0}}
   .ha text{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.12em;fill:#595959}
   @media (prefers-reduced-motion:reduce){.ha *{animation:none!important}.ha .seg,.ha .land{opacity:1}}`;
  for(let i=0;i<segs;i++)css+=kf('g'+i,8+i*6);
  const path0=(l)=>`M${l.x+118} ${l.y} C ${l.x+250} ${l.y}, ${cx-190} ${cy}, ${cx-R-8} ${cy}`;
  return `<svg class="ha" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Abstract animation: sources feed a verification core, one payment lands, a duplicate is stopped">
  <style>${css}</style>
  <defs>
    <linearGradient id="gl" x1="0" x2="1"><stop offset="0" stop-color="#09234f" stop-opacity="0"/><stop offset=".55" stop-color="#384ce3"/><stop offset="1" stop-color="#c4f0ff"/></linearGradient>
    <linearGradient id="gr" x1="0" x2="1"><stop offset="0" stop-color="#c4f0ff"/><stop offset=".5" stop-color="#384ce3"/><stop offset="1" stop-color="#09234f" stop-opacity=".2"/></linearGradient>
    <linearGradient id="gface" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#384ce3" stop-opacity=".55"/><stop offset="1" stop-color="#09234f" stop-opacity=".9"/></linearGradient>
    <linearGradient id="gedge" x1="0" x2="1"><stop offset="0" stop-color="#c4f0ff" stop-opacity=".1"/><stop offset=".5" stop-color="#c4f0ff"/><stop offset="1" stop-color="#384ce3" stop-opacity=".3"/></linearGradient>
    <radialGradient id="gcore"><stop offset="0" stop-color="#c4f0ff"/><stop offset=".45" stop-color="#384ce3"/><stop offset="1" stop-color="#09234f" stop-opacity="0"/></radialGradient>
    <radialGradient id="gfog" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#384ce3" stop-opacity=".09"/><stop offset=".85" stop-color="#384ce3" stop-opacity="0"/></radialGradient>
    <linearGradient id="gseg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#c4f0ff"/><stop offset="1" stop-color="#384ce3"/></linearGradient>
  </defs>
  <rect width="${W}" height="${H}" fill="url(#gfog)"/>
  <!-- depth grid: perspective floor lines fading to the back -->
  ${[0,1,2,3,4,5,6].map(i=>`<line x1="${80+i*160}" y1="${H}" x2="${cx+(i-3)*40}" y2="${cy+40}" stroke="url(#gl)" stroke-opacity="${.10+.04*(3-Math.abs(i-3))}"/>`).join('')}
  <!-- sources -->
  ${layers.slice().reverse().map((l,ri)=>{const i=layers.length-1-ri;return `<g class="lay" style="animation-delay:${-i*.35}s;opacity:${l.o}">
     <path d="${rh(l.x,l.y)}" fill="url(#gface)" stroke="url(#gedge)" stroke-width="1.2"/>
     <path d="${rh(l.x,l.y,70,24)}" fill="none" stroke="#c4f0ff" stroke-opacity=".25"/></g>`}).join('')}
  ${layers.map((l,i)=>`<path d="${path0(l)}" fill="none" stroke="url(#gl)" stroke-width="${1.6-i*.25}" stroke-opacity="${l.o}"/>
     <path class="flow" style="animation-delay:${-i*.4}s" d="${path0(l)}" fill="none" stroke="#c4f0ff" stroke-width="2.2" stroke-linecap="round" stroke-opacity="${l.o}"/>`).join('')}
  <!-- verification core -->
  <circle class="spin" cx="${cx}" cy="${cy}" r="${R+22}" fill="none" stroke="url(#gl)" stroke-dasharray="1 9" stroke-width="1.5"/>
  <circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="#384ce3" stroke-opacity=".15" stroke-width="10"/>
  ${Array.from({length:segs},(_,i)=>`<path class="seg" style="animation-name:g${i}" d="${seg(i)}" fill="none" stroke="url(#gseg)" stroke-width="10" stroke-linecap="butt"/>`).join('')}
  <circle class="core" cx="${cx}" cy="${cy}" r="44" fill="url(#gcore)"/>
  <!-- pay once: one line out, one token lands; a duplicate is stopped at the gate -->
  <path id="pout" d="M${cx+R+8} ${cy} L 860 ${cy}" stroke="url(#gr)" stroke-width="1.6" fill="none"/>
  <path id="pdup" d="M${cx+R+4} ${cy+26} C 680 ${cy+26}, 700 ${cy+46}, 760 ${cy+46}" stroke="url(#gr)" stroke-width="1" stroke-opacity=".45" fill="none"/>
  <line x1="770" y1="${cy+32}" x2="770" y2="${cy+60}" stroke="#09234f" stroke-width="3"/>
  <rect width="12" height="12" x="-6" y="-6" rx="2" fill="#c4f0ff" stroke="#384ce3">
    <animateMotion dur="${T}s" repeatCount="indefinite" keyPoints="0;0;1;1" keyTimes="0;.55;.72;1" calcMode="linear"><mpath href="#pout"/></animateMotion>
    <animate attributeName="opacity" dur="${T}s" repeatCount="indefinite" values="0;0;1;1;0;0" keyTimes="0;.54;.56;.72;.74;1"/></rect>
  <rect width="10" height="10" x="-5" y="-5" rx="2" fill="#384ce3" opacity="0">
    <animateMotion dur="${T}s" repeatCount="indefinite" keyPoints="0;0;1;1" keyTimes="0;.74;.86;1" calcMode="linear"><mpath href="#pdup"/></animateMotion>
    <animate attributeName="opacity" dur="${T}s" repeatCount="indefinite" values="0;0;.9;.9;0;0" keyTimes="0;.73;.75;.86;.92;1"/></rect>
  <g class="land"><path d="${rh(910,cy,62,22)}" fill="url(#gface)" stroke="url(#gedge)" stroke-width="1.2"/>
    <path d="${rh(910,cy+16,62,22)}" fill="none" stroke="#384ce3" stroke-opacity=".35"/></g>
  <circle class="rip" cx="910" cy="${cy}" r="30" fill="none" stroke="#384ce3" stroke-width="1.5"/>
  <text x="210" y="${H-28}" text-anchor="middle">SOURCES</text><text x="${cx}" y="${H-28}" text-anchor="middle">VERIFY</text><text x="910" y="${H-28}" text-anchor="middle">PAY ONCE</text>
  </svg>`;
}


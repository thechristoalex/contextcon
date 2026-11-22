/* Procurement screens (Zoho Procurement flow): Vendors (onboarding), Inbox (uploaded invoices),
   Bills, Payments Made. Loaded before ui.html's main script; uses its helpers at call time
   ($, $$, V, esc, inr, ago, tsf, nice, topbar, toast, busy, ICON, go, ME). */
(function(){
const ZS=s=>`<span class="zst ${esc(s)}">${esc(nice(s))}</span>`;
const R={pass:'pass',warn:'warn',fail:'fail'};
const short=u=>u?String(u).slice(0,8):'—';
const uuidTag=u=>u?`<span class="zuuid" title="${esc(u)} · click to copy" data-copy="${esc(u)}">${esc(short(u))}…</span>`:'<span class="f3">—</span>';
const newKey=()=>(crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(16).slice(2));
const STATE={onboard:{f:'',sel:null},docs:{f:'pending',sel:null},bills:{f:'',sel:null},pmade:{f:'',sel:null}};

async function papi(path,body,key){
  const h={'content-type':'application/json'};if(key)h['Idempotency-Key']=key;
  const r=await fetch(path,body!==undefined?{method:'POST',headers:h,body:JSON.stringify(body)}:{headers:h});
  const j=await r.json();
  if(r.status>=400){const e=new Error(j.error||('HTTP '+r.status));e.data=j;e.status=r.status;throw e}
  return j}

function modal(title,inner,ok='Save',{danger=false}={}){
  return new Promise(res=>{const m=document.createElement('div');m.className='zmodal';
    m.innerHTML=`<div class="bx" role="dialog" aria-modal="true"><div class="hd">${esc(title)}</div><div class="bd">${inner}<div class="zerr" id="zme" style="display:none"></div></div>
      <div class="ft"><button class="btn ghost" data-x>Cancel</button><button class="btn ${danger?'':'primary'}" data-ok style="${danger?'color:var(--bad)':''}">${esc(ok)}</button></div></div>`;
    document.body.appendChild(m);const close=v=>{m.remove();res(v)};
    m.querySelector('[data-x]').onclick=()=>close(null);m.onclick=e=>{if(e.target===m)close(null)};
    m.querySelector('[data-ok]').onclick=()=>{const v={};m.querySelectorAll('[name]').forEach(i=>v[i.name]=i.type==='checkbox'?i.checked:i.value);close(v)};
    setTimeout(()=>m.querySelector('input,textarea,select')?.focus(),0)})}

function errBox(e){const cs=(e.data&&e.data.checks)||[];
  return `<div class="zerr"><b>${esc(e.message)}</b>${cs.length?'<ul style="margin:6px 0 0;padding-left:18px">'+cs.map(c=>`<li>${esc(c.reason)}</li>`).join('')+'</ul>':''}</div>`}
async function act(btn,label,fn,after){return busy(btn,label,async()=>{try{const r=await fn();if(after)await after(r);return r}catch(e){toast(esc(e.message),'err');throw e}})}

function more(items){if(!items.length)return '';
  return `<span class="zmore"><button class="btn" data-more>More ▾</button><span class="m">${items.map(([k,l,d])=>`<button data-a="${k}" class="${d?'danger':''}">${esc(l)}</button>`).join('')}</span></span>`}
function wireMore(root,handler){root.querySelectorAll('[data-more]').forEach(b=>b.onclick=e=>{e.stopPropagation();b.parentElement.classList.toggle('on')});
  root.querySelectorAll('[data-a]').forEach(b=>b.onclick=e=>{e.stopPropagation();b.closest('.zmore')?.classList.remove('on');handler(b.dataset.a,b)});
  document.addEventListener('click',()=>root.querySelectorAll('.zmore.on').forEach(x=>x.classList.remove('on')),{once:true})}

const kv=(k,v)=>`<div class="zkv"><span class="k">${esc(k)}</span><span class="v">${v==null||v===''?'<span class="f3">—</span>':v}</span></div>`;
const checks=(cs,none='No checks run yet.')=>(cs||[]).length?cs.map(c=>`<div class="zchk">${ICON[R[c.result]||'none']}<span class="f2">${esc(nice(c.name))}</span><span>${esc(c.reason)}</span></div>`).join(''):`<div class="f3">${none}</div>`;
const history=h=>(h||[]).length?`<div class="ztl">${h.map(x=>{let d={};try{d=JSON.parse(x.detail||'{}')}catch(e){}
  return `<div><b>${esc(nice(x.action))}</b>${x.to_status?` → ${ZS(x.to_status)}`:''} <span class="f3">· ${tsf(x.at)}${d.by?' · '+esc(d.by):''}${d.reason?' · '+esc(d.reason):''}</span></div>`}).join('')}</div>`:'<div class="f3">No history.</div>';
const SUB=[['onboard','Vendors'],['docs','Uploaded Invoices'],['bills','Bills'],['pmade','Payments Made']];
const subnav=id=>`<div class="zsub">${SUB.map(([k,t])=>`<a class="${k===id?'on':''}" onclick="go('${k}')">${t}</a>`).join('')}</div>`;
const sec=t=>`<div class="sec"><span class="lbl">${esc(t)}</span></div>`;

function shell(key,{views,newBtn='',list,detail}){
  const S=STATE[key];
  return `<div class="chips" style="gap:10px"><select class="zsel" id="zview">${views.map(([v,l])=>`<option value="${v}" ${S.f===v?'selected':''}>${esc(l)}</option>`).join('')}</select><span style="flex:1"></span>${newBtn}</div>
  <div class="zlist ${S.sel?'split':''}" id="zl"><div class="l">${list}</div><div class="d" id="zd">${detail||''}</div></div>`}
function wireList(key,reload,openDetail){
  $('#zview').onchange=e=>{STATE[key].f=e.target.value;STATE[key].sel=null;reload()};
  $$('#zl tr[data-id]').forEach(tr=>tr.onclick=()=>{STATE[key].sel=tr.dataset.id;$$('#zl tr[data-id]').forEach(x=>x.classList.toggle('sel',x===tr));$('#zl').classList.add('split');openDetail(tr.dataset.id)});
  if(STATE[key].sel)openDetail(STATE[key].sel)}
const closeBtn=`<button class="btn ghost sm" data-close title="Close">✕</button>`;
function wireClose(key){$('#zd [data-close]')?.addEventListener('click',()=>{STATE[key].sel=null;$('#zl').classList.remove('split');$$('#zl tr.sel').forEach(x=>x.classList.remove('sel'))})}

/* ================= Vendors (onboarding) ================= */
async function onboard(){
  topbar('Vendors',{crumb:'Procurement'});const S=STATE.onboard;V.innerHTML=skel(5);
  const [list,master]=await Promise.all([papi('/api/procure/vendors'+(S.f&&S.f!=='master'?'?status='+S.f:'')),papi('/api/vendors')]);
  const fixed=master.filter(v=>!v.onboarded).map(v=>({vendor_id:v.vendor_id,display_name:v.legal_name,legal_name:v.legal_name,email:v.contact?.email,gstin:v.gstin,status:'active',fixture:true,failed_checks:0}));
  const rows=S.f==='master'?fixed:S.f===''?[...list,...fixed]:S.f==='active'?[...list,...fixed]:list;
  const views=[['','All Vendors'],['active','Active Vendors'],['invited','Invited Vendors'],['approval_pending','Submitted Vendors (Approval Pending)'],['pending_review','Pending Review'],['rejected','Rejected Vendors'],['inactive','Inactive Vendors'],['master','Vendor master file']];
  const tbl=`<table class="tbl t-vend"><thead><tr><th>Name</th><th>Email</th><th>GSTIN</th><th>Status</th><th class="r">Checks</th></tr></thead><tbody>
    ${rows.map((v,i)=>`<tr class="click rowin ${S.sel===v.vendor_id?'sel':''}" style="animation-delay:${i*15}ms" ${v.fixture?`data-vd="${esc(v.vendor_id)}"`:`data-id="${esc(v.vendor_id)}"`}><td><b>${esc(v.legal_name||v.display_name)}</b><div class="f3 mono">${esc(v.vendor_id)}${v.fixture?' · vendor master file':''}</div></td><td class="f2">${esc(v.email||'—')}</td><td class="mono">${esc(v.gstin||'—')}</td><td>${ZS(v.status)}</td><td class="r">${v.failed_checks?pill('fail',v.failed_checks+' failed'):v.checks_run?pill('pass','passed'):'<span class="f3">—</span>'}</td></tr>`).join('')||`<tr><td colspan="5"><div class="empty"><b>No vendors in this view</b>Invite a vendor: they fill in their own business, address and bank details in the vendor portal.</div></td></tr>`}</tbody></table>`;
  V.innerHTML=subnav('onboard')+shell('onboard',{views,list:tbl,newBtn:`<button class="btn primary" id="vinv">+ Invite Vendor</button><button class="btn" id="vnew">New Vendor</button>`});
  $('#vinv').onclick=async()=>{const f=await modal('Invite Vendor',`<div class="zinfo">The vendor gets a portal link, signs up and submits their organisation details for your approval.</div>
     <label>Display Name*<input name="display_name" placeholder="e.g. Konkan Freight"></label><label>Email Address*<input name="email" type="email" placeholder="accounts@vendor.in"></label>`,'Send Invite');
    if(!f)return;try{const v=await papi('/api/procure/vendors/invite',f);S.sel=v.vendor_id;S.f='';toast('Invitation created. Portal link copied.');navigator.clipboard?.writeText(location.origin+v.portal_link).catch(()=>{});onboard()}catch(e){toast(esc(e.message),'err')}};
  $('#vnew').onclick=async()=>{const f=await modal('New Vendor',`<label>Legal name*<input name="legal_name"></label><label>Email<input name="email"></label><label>GSTIN*<input name="gstin" style="text-transform:uppercase"></label><label>Website<input name="website"></label><label>Billing address*<input name="billing_address"></label><label>Bank account*<input name="bank_account"></label><label>IFSC*<input name="ifsc" style="text-transform:uppercase"></label><label>Account holder*<input name="bank_holder"></label>`,'Save as Draft');
    if(!f)return;try{const v=await papi('/api/procure/vendors',f);S.sel=v.vendor_id;onboard()}catch(e){toast(esc(e.message),'err')}};
  wireList('onboard',onboard,vendorDetail);
  // vendor-master rows have no onboarding record: open the same vendor drawer as the Vendors screen
  $$('#zl tr[data-vd]').forEach(tr=>tr.onclick=()=>window.vendorDrawer&&vendorDrawer(tr.dataset.vd));
}
async function vendorDetail(id){
  const d=$('#zd');d.innerHTML='<div class="zbody">'+skel(4)+'</div>';const v=await papi('/api/procure/vendors/'+encodeURIComponent(id));
  const A=v.allowed_actions,fails=(v.checks||[]).filter(c=>c.result==='fail');
  const primary=A.includes('approve')?`<button class="btn accent" data-do="approve" ${fails.length?'disabled title="Failed checks block approval"':''}>Approve</button>`:A.includes('submit')&&v.status==='draft'?`<button class="btn primary" data-do="submit">Submit for Approval</button>`:A.includes('mark_active')?`<button class="btn" data-do="mark_active">Mark as Active</button>`:'';
  const m=[];if(A.includes('request_more_details'))m.push(['request_more_details','Request More Details']);if(A.includes('reject'))m.push(['reject','Reject',1]);
  if(A.includes('mark_inactive'))m.push(['mark_inactive','Mark as Inactive',1]);if(A.includes('cancel_invite'))m.push(['cancel_invite','Cancel Invite',1]);if(v.portal_link)m.push(['copy','Copy portal link']);
  const tv=v.trust_verdict;
  d.innerHTML=`<div class="zhead"><h2>${esc(v.legal_name||v.display_name)}</h2>${ZS(v.status)}${primary}${more(m)}${closeBtn}</div><div class="zbody">
   ${v.reason?`<div class="${v.status==='rejected'?'zerr':'zinfo'}" style="margin-bottom:12px">${esc(v.reason)}</div>`:''}
   ${v.status==='invited'?`<div class="zinfo" style="margin-bottom:12px">Waiting for the vendor to sign up. Portal link: <a href="${esc(v.portal_link)}" target="_blank">${esc(location.origin+v.portal_link)}</a></div>`:''}
   ${fails.length&&A.includes('approve')?`<div class="zerr" style="margin-bottom:12px">Approval is blocked until the failed checks are fixed. Use Request More Details to send it back to the vendor.</div>`:''}
   ${sec('Onboarding checks')}${checks(v.checks)}
   ${kv('Vendor check (Crustdata)',tv?`${pill(tv==='CLEAR'?'pass':tv==='RUNNING'?'':tv==='HOLD'?'warn':'fail',nice(tv))}${v.trust_job_id&&tv!=='RUNNING'?` <a href="/cases/${esc(v.trust_job_id)}" target="_blank">report</a>`:''}`:'<span class="f3">Not run (needs a Crustdata key)</span>')}
   ${sec('Business details')}<div class="zgrid">${kv('Display name',esc(v.display_name))}${kv('Legal name',esc(v.legal_name))}${kv('GSTIN',`<span class="mono">${esc(v.gstin||'')}</span>`)}${kv('Website',esc(v.website))}${kv('Director',esc(v.director))}${kv('Currency',esc(v.currency))}${kv('Email',esc(v.email))}${kv('Phone',esc(v.phone))}</div>
   ${sec('Address')}<div class="zgrid">${kv('Billing address',esc(v.billing_address))}${kv('Shipping address',esc(v.shipping_address))}</div>
   ${sec('Bank details')}<div class="zgrid">${kv('Account number',`<span class="mono">${esc(v.bank_account||'')}</span>`)}${kv('IFSC',`<span class="mono">${esc(v.ifsc||'')}</span>`)}${kv('Account holder',esc(v.bank_holder))}${kv('Verified on',esc(v.bank_verified_on))}</div>
   ${sec('History')}${history(v.history)}</div>`;
  wireClose('onboard');bindCopy(d);
  const run=async(a,btn)=>{let body={};
    if(a==='copy'){navigator.clipboard?.writeText(location.origin+v.portal_link);return toast('Portal link copied')}
    if(a==='reject'||a==='request_more_details'){const f=await modal(a==='reject'?'Reject vendor':'Request More Details',`<label>${a==='reject'?'Reason for rejection':'What should the vendor add or fix?'}<textarea name="reason" rows="3"></textarea></label>`,a==='reject'?'Reject':'Send request',{danger:a==='reject'});if(!f)return;body=f}
    try{await papi(`/api/procure/vendors/${encodeURIComponent(id)}/${a}`,body);toast(nice(a)+' done');onboard()}catch(e){d.querySelector('.zbody').insertAdjacentHTML('afterbegin',errBox(e))}};
  d.querySelectorAll('[data-do]').forEach(b=>b.onclick=()=>act(b,'Working…',()=>run(b.dataset.do,b)));wireMore(d,run);
}

/* ================= Inbox: uploaded invoices ================= */
async function docs(){
  topbar('Uploaded Invoices',{crumb:'Procurement'});const S=STATE.docs;V.innerHTML=skel(5);
  const list=await papi('/api/procure/documents'+(S.f?'?status='+S.f:''));
  const views=[['','All Documents'],['pending','Pending Documents'],['accepted','Accepted Documents'],['rejected','Rejected Documents']];
  const tbl=`<table class="tbl t-docs"><thead><tr><th>Document Name</th><th>Uploaded by</th><th>Invoice#</th><th class="r">Amount</th><th>Invoice UUID</th><th>Status</th></tr></thead><tbody>
   ${list.map((x,i)=>{const f=x.checks.filter(c=>c.result==='fail').length;return `<tr class="click rowin ${S.sel===x.doc_uuid?'sel':''}" style="animation-delay:${i*15}ms" data-id="${esc(x.doc_uuid)}"><td><b>${esc(x.file_name)}</b><div class="f3">${esc(x.vendor_name||x.vendor_id)} · ${ago(x.created_at)}</div></td><td class="f2">${esc(x.uploaded_by)}</td><td class="mono">${esc(x.extracted.invoice_no||'—')}</td><td class="r mono num">${inr(x.extracted.amount_inr)}</td><td>${uuidTag(x.invoice_uuid)}</td><td>${ZS(x.status)}${f&&x.status==='pending'?' '+pill('fail',f+' failed'):''}</td></tr>`}).join('')||`<tr><td colspan="6"><div class="empty"><b>No documents here</b>Vendors upload invoices from the vendor portal; you can also upload one on a vendor's behalf.</div></td></tr>`}</tbody></table>`;
  V.innerHTML=subnav('docs')+`<p class="f2" style="margin-top:0;margin-bottom:12px">Invoices uploaded by vendors. Every upload gets its own <b>document UUID</b>. Every invoice gets a fixed <b>invoice UUID</b>, built from the vendor and the normalised invoice number. A resent invoice (INV/2026/0451 vs INV-2026-451) therefore lands on the same UUID and can never become a second bill.</p>`+shell('docs',{views,list:tbl,newBtn:`<button class="btn primary" id="dup">Upload Bills</button>`});
  $('#dup').onclick=uploadForOrg;wireList('docs',docs,docDetail);bindCopy(V);
}
async function readFile(file){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsDataURL(file)})}
async function docDetail(id){
  const d=$('#zd');d.innerHTML='<div class="zbody">'+skel(4)+'</div>';const x=await papi('/api/procure/documents/'+encodeURIComponent(id));
  const e=x.extracted||{},src=e._source||{},fails=x.checks.filter(c=>c.result==='fail'),warns=x.checks.filter(c=>c.result==='warn');
  const pend=x.status==='pending';const fileUrl=`/api/procure/documents/${encodeURIComponent(id)}/file`;
  const prev=x.content_type.startsWith('image/')?`<img src="${fileUrl}" style="max-width:100%;border:1px solid var(--line);border-radius:4px">`:x.content_type==='application/pdf'?`<iframe class="zpreview" src="${fileUrl}"></iframe>`:`<pre class="zpreview txt" id="ztxt">Loading…</pre>`;
  d.innerHTML=`<div class="zhead"><h2>${esc(x.file_name)}</h2>${ZS(x.status)}${pend?`<button class="btn accent" data-do="accept">Convert to Bill</button>`:''}${pend?more([['reject','Reject',1]]):''}${x.bill_uuid?`<button class="btn" data-bill="${esc(x.bill_uuid)}">Open bill</button>`:''}${closeBtn}</div><div class="zbody">
   ${x.reason?`<div class="zerr" style="margin-bottom:12px">Rejected: ${esc(x.reason)}</div>`:''}
   ${pend&&fails.length?`<div class="zerr" style="margin-bottom:12px">${fails.length} check(s) failed. A duplicate invoice, a different bank account or a missing invoice number cannot be converted to a bill. Reject it with the reason.</div>`:''}
   <div class="zgrid">${kv('Document UUID',uuidTag(x.doc_uuid))}${kv('Invoice UUID',uuidTag(x.invoice_uuid))}${kv('Invoice key',`<span class="mono">${esc(x.invoice_key||'—')}</span>`)}${kv('SHA-256',`<span class="mono" title="${esc(x.sha256)}">${esc(x.sha256.slice(0,16))}…</span>`)}${kv('Vendor',esc(x.vendor_name||x.vendor_id))}${kv('Uploaded by',esc(x.uploaded_by))}</div>
   ${x.duplicate_of?kv('Duplicate of',uuidTag(x.duplicate_of)):''}
   ${sec('Read from the invoice')}<div class="zgrid">${['invoice_no','invoice_date','po_no','amount_inr','gstin','account_no','ifsc'].map(k=>kv(nice(k),e[k]==null?null:`<span class="mono">${esc(k==='amount_inr'?inr(e[k]):e[k])}</span>${src[k]?` <span class="f3">· ${esc(src[k])}</span>`:''}`)).join('')}</div>
   ${sec('Checks')}${checks(x.checks)}
   ${sec('Document')}${prev}
   ${sec('Activity')}${history(x.history)}</div>`;
  wireClose('docs');bindCopy(d);
  if($('#ztxt'))fetch(fileUrl).then(r=>r.text()).then(t=>$('#ztxt').textContent=t);
  d.querySelector('[data-bill]')?.addEventListener('click',e=>{STATE.bills.sel=e.target.dataset.bill;STATE.bills.f='';go('bills')});
  const run=async a=>{try{
    if(a==='reject'){const f=await modal('Reject document',`<label>Reason (the vendor sees this)<textarea name="reason" rows="3"></textarea></label>`,'Reject',{danger:true});if(!f)return;await papi(`/api/procure/documents/${id}/reject`,f);toast('Document rejected')}
    if(a==='accept'){const f=await modal('Convert to Bill',`${warns.length||fails.length?`<div class="zinfo">${[...fails,...warns].map(c=>'• '+esc(c.reason)).join('<br>')}</div><label>Why is this bill still correct? (required)<textarea name="note" rows="2"></textarea></label>`:'<div class="zinfo">All checks passed. The bill goes to Pending Approval.</div><input type="hidden" name="note" value="">'}<label>Due date<input type="date" name="due_date" value=""></label>`,'Convert to Bill');if(!f)return;
      const b=await papi(`/api/procure/documents/${id}/accept`,f);toast('Bill created: '+esc(b.bill_no))}
    docs()}catch(e){d.querySelector('.zbody').insertAdjacentHTML('afterbegin',errBox(e))}};
  d.querySelectorAll('[data-do]').forEach(b=>b.onclick=()=>act(b,'Working…',()=>run(b.dataset.do)));wireMore(d,run);
}

/* ================= Bills ================= */
async function bills(){
  topbar('Bills',{crumb:'Procurement'});const S=STATE.bills;V.innerHTML=skel(5);
  const list=await papi('/api/procure/bills'+(S.f?'?status='+S.f:''));
  const views=[['','All Bills'],['pending_approval','Pending Approval'],['open','Open'],['overdue','Overdue'],['partially_paid','Partially Paid'],['paid','Paid'],['void','Void']];
  const tbl=`<table class="tbl t-bills"><thead><tr><th>Date</th><th>Bill#</th><th>Vendor</th><th>Status</th><th>Due Date</th><th class="r">Amount</th><th class="r">Balance Due</th></tr></thead><tbody>
   ${list.map((b,i)=>`<tr class="click rowin ${S.sel===b.bill_uuid?'sel':''}" style="animation-delay:${i*15}ms" data-id="${esc(b.bill_uuid)}"><td class="f2">${esc(b.bill_date||'—')}</td><td class="mono">${esc(b.bill_no)}</td><td>${esc(b.vendor_name||b.vendor_id)}</td><td>${ZS(b.display_status)}</td><td class="f2">${esc(b.due_date||'—')}</td><td class="r mono num">${inr(b.amount)}</td><td class="r mono num">${inr(b.balance)}</td></tr>`).join('')||`<tr><td colspan="7"><div class="empty"><b>No bills</b>Bills are created by converting an uploaded invoice in the Inbox.</div></td></tr>`}</tbody></table>`;
  V.innerHTML=subnav('bills')+shell('bills',{views,list:tbl,newBtn:`<button class="btn" onclick="go('docs')">Go to Inbox</button>`});wireList('bills',bills,billDetail);
}
async function billDetail(id){
  const d=$('#zd');d.innerHTML='<div class="zbody">'+skel(4)+'</div>';const b=await papi('/api/procure/bills/'+encodeURIComponent(id));const A=b.allowed_actions;
  const prim=A.includes('approve')?`<button class="btn accent" data-do="approve">Approve</button>`:A.includes('submit')?`<button class="btn primary" data-do="submit">Submit for Approval</button>`:['open','partially_paid'].includes(b.status)?`<button class="btn accent" data-pay>Record Payment</button>`:'';
  const m=[];if(A.includes('reject'))m.push(['reject','Reject',1]);if(A.includes('void'))m.push(['void','Void',1]);if(A.includes('convert_to_draft'))m.push(['convert_to_draft','Convert to Draft']);
  d.innerHTML=`<div class="zhead"><h2>Bill ${esc(b.bill_no)}</h2>${ZS(b.display_status)}${prim}${more(m)}${closeBtn}</div><div class="zbody">
   ${b.reason?`<div class="zerr" style="margin-bottom:12px">${esc(b.reason)}</div>`:''}
   <div class="kpis" style="grid-template-columns:repeat(3,1fr)"><div class="kpi"><span class="lbl">Total</span><div class="v">${inr(b.amount)}</div></div><div class="kpi"><span class="lbl">Balance due</span><div class="v">${inr(b.balance)}</div></div><div class="kpi"><span class="lbl">Due</span><div class="v" style="font-size:18px">${esc(b.due_date||'—')}</div></div></div>
   <div class="zgrid">${kv('Bill UUID',uuidTag(b.bill_uuid))}${kv('Invoice key',`<span class="mono">${esc(b.invoice_key)}</span>`)}${kv('Vendor',esc(b.vendor_name||b.vendor_id))}${kv('PO#',esc(b.po_no))}${kv('Bill date',esc(b.bill_date))}${kv('Source document',b.doc_uuid?`<a href="#" data-doc="${esc(b.doc_uuid)}">${esc(short(b.doc_uuid))}…</a>`:null)}${kv('Bank on file at acceptance',`<span class="mono">${esc((b.vendor_bank||'').replace('|',' / '))}</span>`)}${kv('Approved by',esc(b.approved_by))}${kv('Note',esc(b.note))}</div>
   ${sec('Payments made')}${(b.payments||[]).length?`<table class="tbl"><thead><tr><th>Payment#</th><th>Status</th><th>Date</th><th class="r">Applied</th><th>Bank ref</th></tr></thead><tbody>${b.payments.map(p=>`<tr class="click" data-p="${esc(p.payment_id)}"><td class="mono">${esc(p.payment_no)}</td><td>${ZS(p.status)}</td><td class="f2">${esc(p.payment_date)}</td><td class="r mono">${inr(p.amount)}</td><td class="mono f2">${esc(p.bank_ref||'—')}</td></tr>`).join('')}</tbody></table>`:'<div class="f3">No payments yet.</div>'}
   ${sec('History')}${history(b.history)}</div>`;
  wireClose('bills');bindCopy(d);
  d.querySelector('[data-doc]')?.addEventListener('click',e=>{e.preventDefault();STATE.docs.sel=b.doc_uuid;STATE.docs.f='';go('docs')});
  d.querySelectorAll('[data-p]').forEach(r=>r.onclick=()=>{STATE.pmade.sel=r.dataset.p;STATE.pmade.f='';go('pmade')});
  d.querySelector('[data-pay]')?.addEventListener('click',()=>go('pmade',{vendor_id:b.vendor_id,bill_uuid:b.bill_uuid}));
  const run=async a=>{let body={};if(a==='reject'||a==='void'){const f=await modal(a==='reject'?'Reject bill':'Void bill',`<label>Reason<textarea name="reason" rows="2"></textarea></label>`,a==='reject'?'Reject':'Void it',{danger:true});if(!f)return;body=f}
    try{await papi(`/api/procure/bills/${id}/${a}`,body);toast(nice(a)+' done');bills()}catch(e){d.querySelector('.zbody').insertAdjacentHTML('afterbegin',errBox(e))}};
  d.querySelectorAll('[data-do]').forEach(x=>x.onclick=()=>act(x,'Working…',()=>run(x.dataset.do)));wireMore(d,run);
}

/* ================= Payments Made ================= */
async function pmade(pre){
  if(pre&&pre.vendor_id)return paymentForm(pre);
  topbar('Payments Made',{crumb:'Procurement'});const S=STATE.pmade;V.innerHTML=skel(5);
  const list=await papi('/api/procure/payments'+(S.f?'?status='+S.f:''));
  const views=[['','All Payments'],['draft','Draft'],['pending_approval','Pending Approval'],['processing','Processing'],['paid','Paid'],['rejected','Rejected'],['void','Void']];
  const tbl=`<table class="tbl t-pay"><thead><tr><th>Date</th><th>Payment#</th><th>Vendor</th><th>Bills</th><th>Mode</th><th>Status</th><th class="r">Amount</th></tr></thead><tbody>
   ${list.map((p,i)=>`<tr class="click rowin ${S.sel===p.payment_id?'sel':''}" style="animation-delay:${i*15}ms" data-id="${esc(p.payment_id)}"><td class="f2">${esc(p.payment_date)}</td><td class="mono">${esc(p.payment_no)}</td><td>${esc(p.vendor_name||p.vendor_id)}</td><td class="mono f2">${p.allocations.map(a=>esc(a.bill_no)).join(', ')}</td><td class="f2">${esc(p.mode)}</td><td>${ZS(p.status)}</td><td class="r mono num">${inr(p.amount)}</td></tr>`).join('')||`<tr><td colspan="7"><div class="empty"><b>No payments recorded</b>Record a payment against a vendor's open bills.</div></td></tr>`}</tbody></table>`;
  V.innerHTML=subnav('pmade')+`<p class="f2" style="margin-top:0;margin-bottom:12px">Each payment carries an <b>Idempotency-Key</b>. A double-click, a retry or a crash cannot pay a bill twice: a repeated request returns the first answer, and the bank receives one transfer per payment.</p>`+shell('pmade',{views,list:tbl,newBtn:`<button class="btn primary" id="pnew">+ New</button>`});
  $('#pnew').onclick=()=>paymentForm({});wireList('pmade',()=>pmade(),payDetail);
}
async function paymentForm(pre){
  topbar('Record Payment',{crumb:`<a href="#" onclick="go('pmade');return false">Payments Made</a>`});
  const KEY=newKey();const vs=await papi('/api/vendors');
  V.innerHTML=`<div style="max-width:900px">
   <div class="zgrid" style="margin-bottom:12px"><label class="f2" style="display:grid;gap:4px">Vendor Name*<select id="pv"><option value="">Select a vendor</option>${vs.map(v=>`<option value="${esc(v.vendor_id)}" ${pre.vendor_id===v.vendor_id?'selected':''}>${esc(v.legal_name)}</option>`).join('')}</select></label>
   <label class="f2" style="display:grid;gap:4px">Payment Date<input id="pd" type="date" value="${new Date().toISOString().slice(0,10)}"></label>
   <label class="f2" style="display:grid;gap:4px">Payment Mode<select id="pm"><option>Bank Transfer</option><option>NEFT</option><option>RTGS</option><option>IMPS</option><option>Cheque</option></select></label>
   <label class="f2" style="display:grid;gap:4px">Paid Through<select id="pt"><option>Current Account</option><option>Petty Cash</option></select></label>
   <label class="f2" style="display:grid;gap:4px">Reference#<input id="pr"></label></div>
   <div id="pbills"><div class="f3">Choose a vendor to see their unpaid bills.</div></div>
   <div id="perr"></div>
   <div style="display:flex;gap:8px;margin-top:16px;align-items:center;flex-wrap:wrap"><button class="btn" id="psd">Save as Draft</button><button class="btn primary" id="pss">Save and Submit</button><button class="btn ghost" onclick="go('pmade')">Cancel</button><span class="f3 mono" style="margin-left:auto" title="Sent with this form; reused if you retry">Idempotency-Key ${esc(KEY.slice(0,8))}…</span></div></div>`;
  const load=async()=>{const vid=$('#pv').value;if(!vid){$('#pbills').innerHTML='<div class="f3">Choose a vendor to see their unpaid bills.</div>';return}
    const bs=await papi('/api/procure/unpaid-bills?vendor_id='+encodeURIComponent(vid));
    $('#pbills').innerHTML=bs.length?`<table class="tbl"><thead><tr><th>Date</th><th>Bill#</th><th>Due Date</th><th class="r">Bill Amount</th><th class="r">Amount Due</th><th class="r">In other payments</th><th class="r">Payment</th></tr></thead><tbody>
      ${bs.map(b=>`<tr><td class="f2">${esc(b.bill_date||'—')}</td><td class="mono">${esc(b.bill_no)}</td><td class="f2">${esc(b.due_date||'—')}</td><td class="r mono">${inr(b.amount)}</td><td class="r mono">${inr(b.balance)}</td><td class="r mono ${b.reserved?'':'f3'}">${inr(b.reserved)}</td>
      <td class="r"><input class="pa mono" data-b="${esc(b.bill_uuid)}" data-max="${b.payable_now}" style="width:120px;text-align:right" value="${pre.bill_uuid===b.bill_uuid?b.payable_now:''}"> <a href="#" data-full="${esc(b.bill_uuid)}" class="f3" style="font-size:11px">Pay in Full</a></td></tr>`).join('')}</tbody>
      <tfoot><tr><td colspan="6" class="r"><b>Amount Paid</b></td><td class="r mono"><b id="ptot">₹0.00</b></td></tr></tfoot></table>`:'<div class="empty"><b>No unpaid bills for this vendor</b>Only approved (open) bills can be paid.</div>';
    const tot=()=>{$('#ptot')&&($('#ptot').textContent=inr($$('.pa').reduce((s,i)=>s+(+i.value||0),0)))};
    $$('.pa').forEach(i=>i.oninput=tot);$$('[data-full]').forEach(a=>a.onclick=e=>{e.preventDefault();const i=$(`.pa[data-b="${a.dataset.full}"]`);i.value=i.dataset.max;tot()});tot()};
  $('#pv').onchange=load;await load();
  const save=submit=>async()=>{$('#perr').innerHTML='';
    const body={vendor_id:$('#pv').value,payment_date:$('#pd').value,mode:$('#pm').value,paid_through:$('#pt').value,reference:$('#pr').value||null,submit,
      allocations:$$('.pa').filter(i=>+i.value>0).map(i=>({bill_uuid:i.dataset.b,amount:+i.value}))};
    try{const p=await papi('/api/procure/payments',body,KEY);toast(p.idempotent_replay?'Already recorded: showing the first result':(submit?'Submitted for approval':'Saved as draft'));STATE.pmade.sel=p.payment_id;STATE.pmade.f='';go('pmade')}
    catch(e){$('#perr').innerHTML=errBox(e)}};
  $('#psd').onclick=e=>act(e.currentTarget,'Saving…',save(false));$('#pss').onclick=e=>act(e.currentTarget,'Saving…',save(true));
}
async function payDetail(id){
  const d=$('#zd');d.innerHTML='<div class="zbody">'+skel(4)+'</div>';const p=await papi('/api/procure/payments/'+encodeURIComponent(id));const A=p.allowed_actions;
  const KEY=newKey();const warns=(p.checks||[]).filter(c=>c.result==='warn');
  const m=[];if(A.includes('reject'))m.push(['reject','Reject',1]);if(A.includes('void'))m.push(['void','Void',1]);if(A.includes('convert_to_draft'))m.push(['convert_to_draft','Convert to Draft']);
  d.innerHTML=`<div class="zhead"><h2>${esc(p.payment_no)} · ${inr(p.amount)}</h2>${ZS(p.status)}${A.includes('submit')?`<button class="btn primary" data-do="submit">Submit for Approval</button>`:''}${more(m)}${closeBtn}</div><div class="zbody">
   ${p.reason?`<div class="${p.status==='rejected'?'zerr':'zinfo'}" style="margin-bottom:12px">${esc(p.reason)}</div>`:''}
   ${A.includes('approve')?`${warns.length?`<div class="zinfo" style="margin-bottom:8px">${warns.map(c=>'• '+esc(c.reason)).join('<br>')}</div><textarea id="anote" rows="2" placeholder="Approver note (required: say why this is not a duplicate)" style="width:100%;margin-bottom:8px"></textarea>`:''}
     <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px"><button class="hold" id="zhold"><i class="fill"></i><span id="zholdl">Hold to approve and pay ${inr(p.amount)}</span></button><span class="f3">Pays ${esc(p.vendor_name||p.vendor_id)}'s verified account. Recorded as <b class="f2">${esc(ME&&ME.name||'AP lead')}</b>.</span></div>`:''}
   <div class="zgrid">${kv('Payment ID (bank idempotency key)',uuidTag(p.payment_id))}${kv('Vendor',esc(p.vendor_name||p.vendor_id))}${kv('Payment date',esc(p.payment_date))}${kv('Mode',esc(p.mode))}${kv('Paid through',esc(p.paid_through))}${kv('Reference#',esc(p.reference))}${kv('Created by',esc(p.created_by))}${kv('Approved by',esc(p.approved_by))}${kv('Approver note',esc(p.approver_note))}</div>
   ${p.pay_to_account?sec('Paid to (verified account, snapshot at approval)')+`<div class="zgrid">${kv('Beneficiary',esc(p.pay_to_name))}${kv('Account / IFSC',`<span class="mono">${esc(p.pay_to_account)} / ${esc(p.pay_to_ifsc)}</span>`)}${kv('Bank reference',`<span class="mono">${esc(p.bank_ref||'—')}</span>`)}${kv('Bank transfers for this key',p.bank_transfer?'1':'0')}</div>`:''}
   ${sec('Bills paid')}<table class="tbl"><thead><tr><th>Bill#</th><th>Bill status</th><th class="r">Bill amount</th><th class="r">Balance</th><th class="r">Applied here</th></tr></thead><tbody>${p.allocations.map(a=>`<tr class="click" data-b="${esc(a.bill_uuid)}"><td class="mono">${esc(a.bill_no)}</td><td>${ZS(a.bill_status)}</td><td class="r mono">${inr(a.bill_amount)}</td><td class="r mono">${inr(a.balance)}</td><td class="r mono">${inr(a.amount)}</td></tr>`).join('')}</tbody></table>
   ${sec('Duplicate-payment checks')}${checks(p.checks,'All passed: the vendor is active with a verified account, every bill is open, no amount exceeds what is owed after other payments in flight, and no recent payment to this vendor has the same amount.')}
   ${sec('History')}${history(p.history)}</div>`;
  wireClose('pmade');bindCopy(d);
  d.querySelectorAll('[data-b]').forEach(r=>r.onclick=()=>{STATE.bills.sel=r.dataset.b;STATE.bills.f='';go('bills')});
  const run=async a=>{let body={};if(a==='reject'||a==='void'){const f=await modal(a==='reject'?'Reject payment':'Void payment',`<label>Reason<textarea name="reason" rows="2"></textarea></label>`,a==='reject'?'Reject':'Void it',{danger:true});if(!f)return;body=f}
    try{await papi(`/api/procure/payments/${id}/${a}`,body);toast(nice(a)+' done');pmade()}catch(e){d.querySelector('.zbody').insertAdjacentHTML('afterbegin',errBox(e))}};
  d.querySelectorAll('[data-do]').forEach(x=>x.onclick=()=>act(x,'Working…',()=>run(x.dataset.do)));wireMore(d,run);
  const b=$('#zhold');if(b){let tm=null;const label=`Hold to approve and pay ${inr(p.amount)}`;
    const start=e=>{e.preventDefault();if(b.classList.contains('done'))return;b.classList.add('holding');$('#zholdl').textContent='Keep holding…';
      tm=setTimeout(async()=>{b.classList.remove('holding');b.classList.add('done');$('#zholdl').textContent='Approved · paying…';
        try{const r=await papi(`/api/procure/payments/${id}/approve`,{note:$('#anote')?.value||''},KEY);
          toast(r.result&&r.result.held?'Held: '+esc(r.result.reason):r.idempotent_replay?'Already approved: no second payment':'Paid · '+esc(r.bank_ref||''));pmade()}
        catch(e){b.classList.remove('done');$('#zholdl').textContent=label;d.querySelector('.zbody').insertAdjacentHTML('afterbegin',errBox(e))}},1200)};
    const cancel=()=>{if(b.classList.contains('done'))return;clearTimeout(tm);b.classList.remove('holding');$('#zholdl').textContent=label};
    b.addEventListener('pointerdown',start);['pointerup','pointerleave'].forEach(ev=>b.addEventListener(ev,cancel))}
}

/* the org-side upload: the file chosen inside the modal is captured before the modal closes */
async function uploadForOrg(){
  const vs=await papi('/api/vendors');let picked=null;
  const p=modal('Upload Bills',`<label>Vendor*<select name="vendor_id">${vs.map(v=>`<option value="${esc(v.vendor_id)}">${esc(v.legal_name)} (${esc(v.vendor_id)})</option>`).join('')}</select></label>
   <label>File* (PDF, image or text, up to 5 MB)<input type="file" id="zfile" accept=".pdf,image/*,.txt,.eml"></label>
   <div class="zgrid"><label>Invoice#<input name="invoice_no"></label><label>Amount (₹)<input name="amount_inr" inputmode="decimal"></label><label>PO#<input name="po_no"></label><label>Due date<input name="due_date" type="date"></label></div>
   <div class="f3">Fields are read from the file. Anything typed here is checked against it.</div>`,'Upload');
  setTimeout(()=>{const i=document.getElementById('zfile');if(i)i.onchange=()=>picked=i.files[0]},0);
  const f=await p;if(!f)return;if(!picked)return toast('Choose a file','err');
  try{const r=await papi('/api/procure/documents',{...f,amount_inr:f.amount_inr?+f.amount_inr:null,name:picked.name,data:await readFile(picked)});
    toast(r.deduplicated?'Same file was already uploaded: showing it':'Uploaded · invoice UUID '+esc(short(r.document.invoice_uuid)));STATE.docs.sel=r.doc_uuid;STATE.docs.f='';docs()}
  catch(e){toast(esc(e.message),'err')}}

window.PROC_VIEWS={onboard,docs,bills,pmade};
window.PROC_SCREENS=[{id:'onboard',t:'Procurement',k:'o',tab:1},{id:'docs',t:'Uploaded Invoices',k:'u'},{id:'bills',t:'Bills',k:'n'},{id:'pmade',t:'Payments Made',k:'m'}];
window.PROC_TABOF={onboard:'onboard',docs:'onboard',bills:'onboard',pmade:'onboard'};
})();

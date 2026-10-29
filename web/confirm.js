const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let busy=false;
function render(v){
  const s=v.summary,card=$('#card');
  $('#summary').textContent=s.store+' · '+s.way+' · 实付 ¥'+s.price_yuan;
  if(v.state!=='pending'){if(v.state==='placing'||v.state==='creating')setTimeout(load,800);
    const text={approved:'已确认，订单正在创建。回到对话看结果。',placing:'已确认，正在创建订单……',declined:'已取消，没有创建订单。',used:'已确认，订单已创建。回到对话看付款信息；不想要就点下面的“撤回这单”，或在对话里说一声，会立即撤回。',failed:'没有下成，订单没有创建，也没有扣钱。回到对话看原因。',revoked:'这次下单已撤回，订单没有创建，也没有扣钱。',failure_reported:'没有下成，订单没有创建，也没有扣钱。回到对话看原因。',expired:'这个确认链接已超过有效期，没有创建订单，也没有扣钱。要下单请回到对话重新确认。'}[v.state]||'这个确认已失效。';
    const real={awaiting_payment:'订单已创建，等待付款。回到对话看付款信息；不想要就点下面的“撤回这单”，或在对话里说一声，会立即撤回。',paid:'订单已付款，没有待办。取餐信息回到对话或付款页查看。',partial:'部分订单已付款，其余已撤回或已过期。详情在付款页。',withdrawn:'这笔订单已撤回，没有扣钱。',expired:'这笔订单超过付款时间，已过期撤回，没有付款也没有扣钱。',failed:'没有下成，订单没有创建，也没有扣钱。回到对话看原因。',verifying:'下单结果还在向麦当劳核对中，请不要重新下单。回到对话看结果。'}[v.order_state];
    const shown=(v.state==='used'||v.state==='failure_reported'||v.state==='creating')&&real?real:text;
    // A withdrawn / expired / failed order has nothing left to pay: no payment link, only the result.
    const link=v.payment_path&&['awaiting_payment','verifying','partial','paid'].includes(v.order_state)?`<p><a href="${esc(v.payment_path)}">打开付款页</a></p>`:'';
    const live=['awaiting_payment','verifying'].includes(v.order_state)||['approved','placing','creating'].includes(v.state);
    const withdrawBtn=live?`<div class="row"><button id="withdraw" class="withdraw">撤回这单</button></div>`:'';
    card.innerHTML=`<h2>${esc(s.store)}</h2><p class="done">${shown}</p>${link}${withdrawBtn}`;if(live)$('#withdraw').onclick=()=>act('withdraw');$('#note').hidden=true;$('h1').textContent=({withdrawn:'已撤回',expired:'已过期',paid:'已付款'}[v.order_state])||'已处理';return;
  }
  card.innerHTML=`<h2>${esc(s.store)} · ${esc(s.way)}</h2><ul>${s.lines.map(r=>`<li><span>${esc(r.name)} ×${r.quantity}</span><span>¥${esc(r.amount_yuan)}</span></li>`).join('')}</ul><div class="total"><span>实付</span><span>¥${esc(s.price_yuan)}</span></div><p>${esc(s.payments_note)}。</p>${s.real?'<p class="real">点击后会在麦当劳创建<b>真实</b>的待付款订单，需要你在 15 分钟内用手机付款，否则自动撤回。</p>':'<p class="sim">当前是模拟模式，不会产生真实订单。</p>'}<div class="row"><button class="go" id="go">确认下单 ¥${esc(s.price_yuan)}</button><button id="no" class="withdraw">撤回这单</button></div>`;
  $('#go').onclick=()=>act('approve');$('#no').onclick=()=>act('withdraw');
}
async function act(kind){
  if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{const r=await fetch(location.pathname+'/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error('rejected');}
  catch{$('#error').textContent='没有提交成功，请刷新页面重试。'}
  busy=false;await load();
}
async function load(){
  try{const r=await fetch(location.pathname+'/state',{cache:'no-store'}),v=await r.json();if(!r.ok)throw Error(v.error||'失效');$('#error').textContent='';render(v)}
  catch(e){$('#card').innerHTML='';$('#summary').textContent='这个确认链接已失效，请回到对话重新确认。'}
}
load();setInterval(()=>{if(!busy)load()},3000);

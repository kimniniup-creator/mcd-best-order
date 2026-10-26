const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let busy=false;
function render(v){
  const s=v.summary,card=$('#card');
  $('#summary').textContent=s.store+' · '+s.way+' · 实付 ¥'+s.price_yuan;
  if(v.state!=='pending'){
    const text={approved:'已确认。回到对话，订单马上创建。',declined:'已取消，没有创建订单。',used:'这次确认已经使用，订单已创建。',expired:'这个确认已过期，请回到对话重新确认。'}[v.state]||'这个确认已失效。';
    card.innerHTML=`<h2>${esc(s.store)}</h2><p class="done">${text}</p>`;$('h1').textContent='已处理';return;
  }
  card.innerHTML=`<h2>${esc(s.store)} · ${esc(s.way)}</h2><ul>${s.lines.map(r=>`<li><span>${esc(r.name)} ×${r.quantity}</span><span>¥${esc(r.amount_yuan)}</span></li>`).join('')}</ul><div class="total"><span>实付</span><span>¥${esc(s.price_yuan)}</span></div><p>${esc(s.payments_note)}。</p>${s.real?'<p class="real">点击后会在麦当劳创建<b>真实</b>的待付款订单，需要你在 15 分钟内用手机付款，否则自动撤回。</p>':'<p class="sim">当前是模拟模式，不会产生真实订单。</p>'}<div class="row"><button class="go" id="go">确认下单 ¥${esc(s.price_yuan)}</button><button id="no">先不下单</button></div>`;
  $('#go').onclick=()=>act('approve');$('#no').onclick=()=>act('decline');
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

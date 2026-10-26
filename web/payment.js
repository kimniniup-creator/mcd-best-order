const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let latest,receivedAt=0,tickTimer;
function tick(){if(!latest)return;document.querySelectorAll('[data-countdown]').forEach(el=>{const o=latest.orders[+el.dataset.countdown],seconds=Math.max(0,o.remaining_seconds-Math.floor((Date.now()-receivedAt)/1000));el.textContent=`剩余 ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`})}
async function poll(){try{
  const r=await fetch(location.pathname+'/state',{cache:'no-store'}),s=await r.json();
  if(!r.ok)throw Error('status unavailable');
  latest=s;receivedAt=Date.now();clearInterval(tickTimer);tickTimer=setInterval(tick,1000);
  $('#summary').textContent=s.store+' · '+s.summary+(s.dry_run?' · 模拟订单，不能付款':'');
  const pending=s.orders.some(o=>o.state==='pending');
  $('h1').textContent=pending?'在麦当劳 App 完成付款':s.notice?'订单部分完成':'订单状态已更新';
  $('.steps').textContent=s.notice||(pending?(s.device==='mobile'?'请先安装麦当劳 App，点下方按钮打开 App 付款。若未成功打开，可在微信麦当劳小程序 →「我的订单」查找本单（能否直接付款待实测；App 订单页可能看不到）。也可用另一台手机扫码。':'必须先安装麦当劳 App。电脑上请用手机麦当劳 App 扫一扫或微信扫一扫扫描下方二维码。也可在微信麦当劳小程序 →「我的订单」查找本单（能否直接付款待实测；App 订单页可能看不到）。'):'本次付款已结束，请查看每笔订单的状态和取餐信息。');
  $('#orders').innerHTML=s.orders.map((o,i)=>`<article class="payment"><h2>第 ${i+1} 笔</h2><div class="amount">¥${(o.amount/100).toFixed(2)}</div><span class="badge badge--${o.state}">${esc(o.status_text)}</span><p class="items">${o.items.map(r=>esc(r.name)+' ×'+r.quantity).join(' · ')}</p>${o.state==='pending'?`${o.mobile_url?`<a class="app" href="${esc(o.mobile_url)}">打开麦当劳 App 付款</a>`:''}<p class="countdown" data-countdown="${i}"></p><div class="qr">${o.qr_svg}</div><p>用手机麦当劳 App 扫一扫</p>`:''}${o.state==='paid'?(o.pickup_code?`<p>取餐码：<b>${esc(o.pickup_code)}</b></p>`:'<p>已付款，取餐码稍后在麦当劳 App 里查看</p>'):''}</article>`).join('');
  $('footer').textContent=pending?'付款后会自动更新，并由对话助手继续告知取餐信息。请保持点餐对话开启，未付款的订单会在到期前自动撤回。钱直接付给麦当劳，我们不经手。':s.notice||'订单状态已更新。已付款的餐品请凭取餐码取餐；暂未显示取餐码时，请稍后在麦当劳 App 里查看。';
  tick();$('#error').textContent=s.orders.some(o=>o.query_error)?'暂时查不到订单状态，正在重试':'';
}catch{$('#error').textContent='暂时查不到订单状态，正在重试'}finally{setTimeout(poll,3000)}}
poll();

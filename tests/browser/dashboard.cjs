// Isolated UI checks: every request is fulfilled locally; no trading service is contacted.
// Run with Playwright installed: node tests/browser/dashboard.cjs
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../../src/asm/services/api/static/index.html'), 'utf8');
const wallet = '7KpXhVY7pRHxNq9SMDLqH3ecMNrTG5UVs53nHBu9Yk7n';
const mint = 'So11111111111111111111111111111111111111112';
const now = new Date().toISOString();
const trades = [{decision:'copy',trader:wallet,mint,size_usd:125,total_copy_latency_ms:480,analysis_latency_ms:120,observation_latency_ms:360,decided_at:now,reason_codes:[]}];
const portfolio = {equity_usd:12485.62,high_water_mark_usd:12600,drawdown_pct:.91,cash_usd:8120.5,deployed_usd:4365.12,deployed_pct:34.96,unrealized_pnl_usd:328.42,realized_pnl_today_usd:186.7,open_positions:3,harvested_total_usd:750,exposure:{by_token:{[mint]:2400,[wallet]:1965.12}}};
const fixtures = {
 '/portfolio':portfolio,
 '/portfolio/performance':{series:Array.from({length:40},(_,i)=>({t:new Date(Date.now()-(39-i)*4*3600000).toISOString(),equity:10000+i*62+Math.sin(i*.7)*170}))},
 '/trades':{trades}, '/trades/stats/rejections':{reasons:[{code:'LOW_LIQUIDITY',count:12},{code:'PRICE_MOVED_TOO_FAR',count:7}]},
 '/setup/wallets':{wallets:[{wallet,status:'active',score:83.2,copyability:78,confidence:85,trade_count:142,source:'manual',last_trade_at:now}],counts:{copyable:1,total:1}},
 '/positions':{positions:[{mint,trader:wallet,status:'open',cost_basis_usd:125,entry_price:142,current_price:149.5,realized_pnl_usd:0,trailing_armed:true,opened_at:now}]},
 '/harvesting':{config:{threshold_pct:25,take_pct:40,min_active_capital_usd:1000,destination:wallet},events:[{at:now,equity_before:13000,harvested_usd:750,retained_usd:12250,equity_after:12250}]},
 '/research':{gates:{signals:128,approved:32,approval_rate_pct:25},latency:{observation_ms:{p50:350},analysis_ms:{p50:120},total_copy_ms:{p95:920},advice:'Latency is within the configured window.'},traders:{contributors:[{wallet,closed_trades:12,realized_pnl_usd:186,capital_deployed_usd:2400,return_on_deployed_pct:7.75}],note:'Performance over the last 30 days.'}},
 '/research/graph':{clusters:[],relationships:[],note:''},
 '/research/credits':{within_budget:true,today:1420,daily_budget:30000,history:[{day:'2026-09-12',total:1420,by_method:{getTransaction:1000,getAccountInfo:420}}],month_projection:42600,monthly_budget:1000000,headroom_pct:95.7,note:''},
 '/config':{sections:[{name:'Position sizing',fields:[{key:'base_position_pct',label:'Base position size',help:'Percentage of equity allocated to a position.',value:'0.75',min:0,max:10,unit:'%',danger:false}]},{name:'Risk limits',fields:[{key:'max_daily_loss_pct',label:'Maximum daily loss',help:'Pause new entries when the daily loss limit is reached.',value:'5',min:1,max:30,unit:'%',danger:true}]}],note:'Changes apply to running services.'},
 '/setup/jobs':{jobs:[{key:'score',label:'Score wallets',detail:'Refresh scores for watched wallets.',cost:'Uses provider credits',schedule:'daily'}]},
};
(async()=>{
 const browser=await chromium.launch({headless:true, ...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
 const errors=[], mutations=[], performanceRequests=[]; let authenticated=false, state='running', failPortfolio=false;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const req=route.request(), url=new URL(req.url()), p=url.pathname;
  if(p==='/portfolio/performance')performanceRequests.push(url.searchParams.get('hours'));
  let status=200,body={ok:true};
  if(p==='/') return route.fulfill({status:200,contentType:'text/html',body:html});
  if(p==='/auth/check'){status=authenticated?200:401;body={ok:authenticated};}
  else if(p==='/auth/login'){authenticated=req.postDataJSON().password==='preview-password';status=authenticated?200:401;body={detail:'Incorrect password.'};}
  else if(p==='/auth/logout'){authenticated=false;}
  else if(p==='/health')body={mode:'paper',system_state:state,started:true,equity_usd:portfolio.equity_usd};
  else if(req.method()==='POST'){mutations.push(p);if(p==='/system/pause')state='soft_paused'; if(p==='/system/resume')state='running';}
  else if(p==='/portfolio'&&failPortfolio){status=503;body={};}
  else if(fixtures[p])body=fixtures[p];
  else {status=404;body={error:'Unknown mock endpoint '+p};}
  await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
 });
 try{
  await page.goto('http://dashboard.test/');
  if(process.env.SCREENSHOT_DIR){fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'dashboard-login.png')});}
  await page.locator('#pw').fill('wrong');await page.locator('#unlock').click();
  await page.getByText('Incorrect password.',{exact:true}).waitFor();
  await page.locator('#pw').fill('preview-password');await page.locator('#unlock').click();
  await page.locator('#curve').waitFor();
  const widths=[360,390,768,1024,1440];
  for(const width of widths){
   await page.setViewportSize({width,height:1000});
   for(const tab of ['overview','traders','trades','positions','harvest','research','settings']){
    await page.locator(`[data-tab="${tab}"]`).click();
    await page.waitForFunction(()=>document.querySelector('#view').getAttribute('aria-busy')==='false'&&!document.querySelector('#refresh').disabled);
    assert.equal(await page.locator('#error').innerText(),'',`${tab} errors at ${width}`);
    const bounds=await page.evaluate(()=>({body:document.documentElement.scrollWidth,viewport:innerWidth}));
    assert(bounds.body<=bounds.viewport,`${tab} overflows at ${width}: ${JSON.stringify(bounds)}`);
    assert.equal(await page.locator('[aria-current="page"]').count(),1);
    if(process.env.SCREENSHOT_DIR&&width===390&&['settings','traders'].includes(tab))await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,`dashboard-mobile-${tab}.png`),fullPage:true});
   }
  }
  await page.locator('[data-tab="overview"]').click(); await page.locator('#curve').waitFor();
  for(const hours of [24,720,168]){
   await page.locator(`[data-period="${hours}"]`).click();
   await page.waitForFunction(h=>document.querySelector(`[data-period="${h}"]`)?.getAttribute('aria-pressed')==='true'&&document.querySelector('#view').getAttribute('aria-busy')==='false',hours);
   assert.equal(performanceRequests.at(-1),String(hours));
  }
  await page.locator('[data-go="positions"]').click();
  await page.waitForFunction(()=>document.querySelector('#pagetitle').textContent==='Positions'&&document.querySelector('#view').getAttribute('aria-busy')==='false');
  await page.locator('[data-tab="overview"]').click();await page.locator('#curve').waitFor();
  if(process.env.SCREENSHOT_DIR){
   fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});
   for(const [name,width] of [['desktop',1440],['mobile',390]]){
    await page.setViewportSize({width,height:1000});
    await page.waitForTimeout(250);
    await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,`dashboard-${name}.png`),fullPage:true});
   }
  }
  await page.locator('#pause').click();await page.locator('#resume').waitFor();
  await page.locator('#resume').click();await page.locator('#halt').waitFor();
  await page.locator('#halt').click();await page.locator('#modal:not([hidden])').waitFor();
  assert.equal(await page.locator('#mcancel').evaluate(e=>e===document.activeElement),true);
  await page.keyboard.press('Tab');assert.equal(await page.locator('#mok').evaluate(e=>e===document.activeElement),true);
  await page.keyboard.press('Tab');assert.equal(await page.locator('#mcancel').evaluate(e=>e===document.activeElement),true);
  await page.keyboard.press('Escape');assert.equal(await page.locator('#modal').isHidden(),true);
  assert(!mutations.includes('/system/stop'),'Cancellation must not stop trading');
  await page.locator('#estop').click();await page.locator('#mcancel').click();
  assert(!mutations.includes('/system/emergency-stop'));
  failPortfolio=true;await page.locator('#refresh').click();await page.locator('#error .err').waitFor();
  failPortfolio=false;await page.locator('#refresh').click();await page.locator('#curve').waitFor();
  assert.equal(await page.locator('#error').innerText(),'');
  fixtures['/portfolio/performance']={series:[]};fixtures['/trades']={trades:[]};fixtures['/portfolio']={...portfolio,exposure:{by_token:{}}};
  await page.locator('#refresh').click();await page.getByText('Not enough data yet',{exact:true}).waitFor();
  await page.getByText('No decisions yet',{exact:true}).waitFor();
  await page.locator('[data-tab="traders"]').click();await page.locator('#wtext').fill(wallet);
  await page.clock.install();await page.clock.fastForward(21000);
  assert.equal(await page.locator('#wtext').inputValue(),wallet,'Polling must preserve wallet input');
  await page.locator('[data-tab="settings"]').click();await page.locator('[data-cfg="base_position_pct"]').fill('1');
  assert.equal(await page.locator('#cfgsave').isEnabled(),true);
  await page.locator('#signoutmobile').click();await page.locator('#lock').waitFor();
  assert.equal(await page.locator('#pw').inputValue(),'');
  assert.deepEqual(errors,[]);
  console.log('PASS: 7 views × 5 widths; login/logout; chart ranges and position shortcut; empty states; pause/resume; modal focus/cancel; error recovery; draft preservation; settings editing. No real service contacted.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
